---
name: dify2weknora-migration
description: 把 Dify 知识库迁移/重迁/补迁到 WeKnora 知识库的标准流程与自动化脚本。凡用户提到 Dify 导出、迁移知识库、重迁、补迁、建 v2 库、full-export、取子集等即用本技能——即使没说"迁移"二字（如"把股票库重建一遍""把这批文档导进 weknora"）。
requires:
  bins: [bash, docker, rsync]
  python: ">=3.6"
---

# Dify → WeKnora 知识库迁移（dify2weknora-migration）

> 本技能为**通用开源版**：内网部署时先填写 `references/node-map.md` 模板里的
> 环境占位符（各节点地址/路径/访问姿势），技能即与你的环境绑定。

## 第零条硬规则（先于一切）

**生成任何执行命令前，先读实体，再写命令。** 具体动作：

1. 涉及既有脚本 → `head -30 <脚本>` 读参数头（变量名/默认值/用法）；
2. 涉及文件路径 → `ls` 验证实际位置（文档叙述可能与现场漂移）；
3. 涉及数字对账 → 取"预期值"写入命令注释，跑完核对。

**为什么**：真实事故——导出脚本的参数明明在文档和脚本头部都有，却凭记忆写了
一个不存在的环境变量名，导致整库全量误拷。参数从实体读、不从记忆编。

## 标准流程（六段）

### ① 全实例导出（一次性资产，已存在则跳过）

- 脚本：`dify_ah_export.sh`（在 Dify 宿主机的迁移脚本目录，参数以脚本头部为准）
- **它没有按库过滤参数**（典型参数=DB 容器名/SRC=存储路径/STAGE=输出目录/空间闸门），
  本就设计为全实例导出：四表 CSV + 全部引用文件 + 全量 manifest。
- 完成标志：`[N/N] manifest + meta` 打印 + manifest.csv 落盘。
- rsync 增量可续跑：中断后同命令重跑即续。**全量导出是所有批次共用的一次性投资。**

### ② 按库取子集（用封装脚本，勿手拼命令）

- 脚本：`{SKILL_DIR}/scripts/dify_subset_export.sh`
- 用法（argv 优先，env 兜底）：
  `bash {SKILL_DIR}/scripts/dify_subset_export.sh <ds-uuid>[,uuid2] <label>[,label2] <stage-dir> [full-dir] [db-container]`
- 产物：manifest.csv（带 source_kb 血缘列）/ keys.txt（去重）/ files/（keyed 布局子集）
- 数字对账：manifest 行数-1 ≈ keys 行数 ≈ 源库文档数（重复档在 keys 层天然去重）。

### ③ 传输 导出节点 → 导入节点

- rsync 三连（mkdir 目标 / --files-from=keys 拷 files/ / 拷 manifest+keys）
- 文件按 file_key 相对布局落位，导入与制备脚本均按此寻址。

### ④ 元数据制备（导入前）

- 模式：**入库时间全量注入（真值）；撰写时间有则提取、无则留空**
- 提取三层：html 就地扫文件头"发送时间"（真值）→ 文件名正则阶梯（不变量
  2015≤日期≤入库+7 天；非精确形态弃）→ 可选 LLM 仲裁（先看正则覆盖率再决定）
- 审计文件必看：覆盖率+分叉计数+样例；覆盖畸高先查（多半是误匹配）。

### ⑤ 建库（导入节点）

- **新批次一律建独立新库**（名如 `<库名>-v2`）：旧库原样留作回滚资产，
  `--recreate` 互删陷阱即消失。
- 建库载荷要带三件：embedding_model_id + summary_model_id（先 GET /api/v1/models 取）+
  `chunking_config.parser_engine_rules`（xlsx→markitdown 路由：docreader 双引擎注册
  xlsx，KB 级配置即生效）。

### ⑥ 导入 + 验收

- 导入脚本支持 `--metadata-csv` sidecar（doc_id,metadata 两列，metadata=JSON 串）。
  **预建过库的批次不要 --recreate**（会删掉带引擎路由的库）。
- nohup + `python3 -u`（日志不缓冲）+ log 到 /tmp。
- 验收四查：档数（内容去重 409 差额逐条记录）/ 块数（chunks 表直查 by kb uuid）/
  xlsx 档块数数百级（markitdown 路由生效铁证）/ 冒烟检索。
- 元数据搬家：两条独立 UPDATE（入库时间≈总档数、撰写时间=覆盖数），**各自核对
  影响行数并与审计文件对账**。
- 终验以文件 stat Birth/Modify 为准（"秒级假完成"前科：日志竞态读旧完成标记）。

## 已知坑清单（每条都真踩过）

| 坑 | 要点 |
|---|---|
| 巨兽 xlsx | 数据工作簿逐行切块可达数千块/MB；markitdown 路由可治块数爆炸，检索价值仍低需业务判断 |
| 409 去重 | WeKnora 内容哈希去重=重跑安全的特性，非失败 |
| 假完成 | 触发导入前删旧完成日志；终验 stat 不看 log |
| chunks=0 | KB 详情字段口径可能失真，块数以 chunks 表直查为准 |
| metadata 路由 | 上传 metadata 域进内部列，UI 面板读 custom_metadata——导入后必须幂等 UPDATE 搬家 |
| py3.6 老节点 | 无 fromisoformat；f-string 可用 |
| 共享 LLM 端点 | 高峰期批量任务超时：调大 timeout+减小 BATCH |
| --recreate 互删 | 分批导同一库会互删；独立新库名根治 |
| 存储配额 | 多租户批量前必查租户 storage_quota/storage_used |
| 环境叙述漂移 | 文档里的路径/参数可能与现场不符——第零条硬规则兜底 |

## 运行环境

三角色分工（节点细节、地址与访问姿势见 `{SKILL_DIR}/references/node-map.md`，
**首次部署须先填模板占位符**）：

| 角色 | 节点 | 负责段落 |
|---|---|---|
| 作者层 | 设计/验收机（如本地工作站） | 全流程设计/验收判读 |
| 导出节点 | Dify 宿主机 | 第①②③段 |
| 导入节点 | WeKnora 生产机 | 第④⑤⑥段 |

## 长任务清单（执行层须知）

以下步骤耗时远超脚本超时（120s 量级），不得作为"技能脚本"调用，
须以确认闸门放行的 shell 命令形态执行（nohup 挂起 + tail 轮询日志 +
stat 终验——审计例外，操作者知情）：

1. 全实例导出（第①段）：大体积拷贝，数十分钟级，rsync 增量可续跑；
2. 子集文件拷贝（第②段脚本第 3 步）：GB 级分钟量级；
3. 导入（第⑥段）：小时级，nohup 挂起；
4. 元数据制备的 LLM 仲裁（--llm 时）：小时级，同 nohup 形态。

---
name: log-triage
description: 对一个日志文件做快速分诊：按小时统计 ERROR/WARN 数量、归并高频错误模式、抽取代表性样例。当用户给出一个日志文件并希望"看看有没有异常 / 报错主要集中在哪"时使用。
requires:
  bins: [grep, awk, sort, uniq, tail]
metadata:
  owner: example
---

# log-triage

## 何时使用
- 用户给出日志文件路径，想快速定位异常分布。
- 不适合：实时滚动日志（先 tail 落盘再分析）；GB 级日志（先按时间窗口切片）。

## 用法
    bash {SKILL_DIR}/scripts/triage.sh <日志路径> [关键字，默认 ERROR]

- 输出三块：按小时分布、归一化后的高频错误 Top10（数字与路径已打码归并）、代表样例 5 条。
- 全部只读操作；首次执行需用户确认（或用 -a 启动）。
- 大文件可能耗时，脚本默认只分析最后 200 万行以内（内核命令超时可调大 timeout 参数）。

## 解读建议
- 先用"按小时分布"判断故障时间窗，再用 Top10 定位主因，最后用样例确认细节。
- 若输出为空，说明关键字无命中，建议用户换关键字（如 WARN、Exception）。

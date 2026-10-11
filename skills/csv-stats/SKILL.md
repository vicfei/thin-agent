---
name: csv-stats
description: 快速了解 CSV 文件的结构与数据质量（行数、缺失、唯一值、数值范围）。当用户给出 CSV 并希望"看看里面有什么 / 数据质量如何"时使用。
requires:
  python: ">=3.6"
metadata:
  owner: example
---

# csv-stats

## 何时使用
- 用户给出 CSV 路径，想快速了解结构或质量。
- 不适合：需要精确业务口径的正式统计；行数超过约 100 万的文件（先让用户抽样）。

## 用法
    python3 {SKILL_DIR}/scripts/stats.py <csv路径>

- 输出：stdout 上的 JSON，形如 {"rows": N, "columns": {列名: {...}}}。
- 数值列给出 min/max/mean；文本列给出 top3 高频值。
- 退出码非 0 时，读 stderr 向用户解释原因，最多重试一次。
- 该命令不在纯读白名单内，首次执行需要用户确认（或用 -a 启动）。

## 解读建议
- missing 比例高的列，在后续分析前应提示用户。
- 直接基于返回的 JSON 回答，不要回头读取原 CSV 全文。

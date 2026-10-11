#!/usr/bin/env bash
# log-triage：日志分诊。用法: triage.sh <日志路径> [关键字=ERROR]
set -u
LOG="${1:?用法: triage.sh <日志路径> [关键字]}"
KW="${2:-ERROR}"
[ -f "$LOG" ] || { echo "文件不存在: $LOG" >&2; exit 2; }

HITS=$(mktemp)
trap 'rm -f "$HITS"' EXIT
grep -a "$KW" "$LOG" > "$HITS" || { echo "无 '$KW' 命中"; exit 0; }
total=$(wc -l < "$HITS")
echo "== 命中总数: $total =="

echo "== 按小时分布 =="
# 兼容 "2026-10-10 14:23:01" 与 "Oct 10 14:23:01" 两种常见时间戳
awk '{ if (match($0, /[0-9]{2}:[0-9]{2}:[0-9]{2}/)) print substr($0, RSTART, 2) ":00-" substr($0, RSTART, 2) ":59" }' "$HITS" \
  | sort | uniq -c | sort -k2

echo "== 高频错误模式 Top10（数字/路径已归并） =="
sed -e 's/[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}//' -e 's/[0-9]\{2\}:[0-9]\{2\}:[0-9]\{2\}//' \
    -e 's/[0-9]\{1,3\}\(\.[0-9]\{1,3\}\)\{3\}/<IP>/g' -e 's/[0-9]\{2,\}/<N>/g' \
    -e 's|/[A-Za-z0-9_./-]*|<PATH>|g' "$HITS" \
  | cut -c1-160 | sort | uniq -c | sort -rn | head -10

echo "== 代表样例（首尾各取） =="
head -3 "$HITS" | cut -c1-300
echo "..."
tail -2 "$HITS" | cut -c1-300

#!/usr/bin/env bash
# sys-doctor：只读机器体检。纯读命令，无写入、无清理动作。
set -u

cores=$(nproc 2>/dev/null || echo 1)
load1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null || echo 0)

echo "== 概览 =="
uptime 2>/dev/null
echo "CPU 核数: $cores"

echo "== 内存 =="
free -h 2>/dev/null

echo "== 磁盘（使用率≥85% 需关注） =="
df -h -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR==1 || $5+0 >= 0'

echo "== 内存占用 Top5 进程 =="
ps aux --sort=-%mem 2>/dev/null | head -6

echo "== CPU 占用 Top5 进程 =="
ps aux --sort=-%cpu 2>/dev/null | head -6

echo "== 最近登录 =="
last -n 5 2>/dev/null || true

echo "== 初步提示 =="
awk -v l="$load1" -v c="$cores" 'BEGIN{ if (l > c) print "[hint] load1 超过核数，CPU 可能饱和"; else print "[hint] 负载正常" }'
free 2>/dev/null | awk '/^Mem:/{ if ($7/$2 < 0.1) print "[hint] 可用内存不足 10%"; else print "[hint] 内存正常" }'
df -P -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR>1 && $5+0 >= 85 { print "[hint] 分区 " $6 " 使用率 " $5 }'

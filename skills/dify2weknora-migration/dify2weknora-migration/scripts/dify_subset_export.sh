#!/usr/bin/env bash
# dify_subset_export.sh — 从 full-export 全实例导出中提取指定 dataset 的子集批次。
# 参数一律从本头部读取（第零条硬规则的产物）：DS/LABELS/STAGE/FULL/DB。
#
# 用法（.37 root）：
#   DS=uuid1,uuid2 LABELS=email,irp STAGE=/data/workspace/dify-mig/<批名> bash dify_subset_export.sh
# 前置：full-export 已完成（manifest.csv 存在；没有则先跑 dify_ah_export.sh 全实例导出）。
# 产物：$STAGE/manifest.csv（带 source_kb 列）/ $STAGE/keys.txt / $STAGE/files/（keyed 布局）
set -euo pipefail

# 传参双形态：argv 优先，env 兜底
#   argv: bash dify_subset_export.sh <ds-csv> <labels-csv> <stage-dir> [full-dir] [db-container]
#   env : DS=... LABELS=... STAGE=... [FULL=...] [DB=...] bash dify_subset_export.sh
if [ $# -ge 3 ]; then
  DS=$1; LABELS=$2; STAGE=$3
  FULL=${4:-/data/workspace/dify-mig/full-export}
  DB=${5:-docker-db_postgres-1}
else
  DS=${DS:?需要 DS=<uuid[,uuid2...]>（或 argv: <ds> <labels> <stage>）}
  LABELS=${LABELS:?需要 LABELS=<短名[,短名2...]>（与 DS 一一对应）}
  STAGE=${STAGE:?需要 STAGE=<子集输出目录>}
  FULL=${FULL:-/data/workspace/dify-mig/full-export}
  DB=${DB:-dify-db-1}
fi
PSQL="docker exec -i $DB psql -U postgres -d dify"

# 前置校验：full-export 完成标志
[ -f "$FULL/manifest.csv" ] || { echo "ERROR: $FULL/manifest.csv 不存在——先跑 dify_ah_export.sh 全实例导出（完成标志 [6/6]）"; exit 1; }
[ -d "$FULL/files" ] || { echo "ERROR: $FULL/files 不存在"; exit 1; }

# DS/LABELS 数量一致性
IFS=',' read -r -a DS_ARR <<< "$DS"
IFS=',' read -r -a LB_ARR <<< "$LABELS"
[ ${#DS_ARR[@]} -eq ${#LB_ARR[@]} ] || { echo "ERROR: DS(${#DS_ARR[@]}) 与 LABELS(${#LB_ARR[@]}) 数量不一致"; exit 1; }

# 拼 IN 列表与 source_kb CASE 表达式
IN_LIST="'${DS_ARR[0]}'"
CASE_EXPR="case ds.id when '${DS_ARR[0]}' then '${LB_ARR[0]}'"
for i in $(seq 1 $((${#DS_ARR[@]} - 1))); do
  IN_LIST="$IN_LIST,'${DS_ARR[$i]}'"
  CASE_EXPR="$CASE_EXPR when '${DS_ARR[$i]}' then '${LB_ARR[$i]}'"
done
CASE_EXPR="$CASE_EXPR else 'other' end"

mkdir -p "$STAGE/files"

echo "[1/3] 子集 manifest（带 source_kb）"
$PSQL -c "\copy (select d.id as doc_id, ds.name as dataset_name, d.name as doc_name, d.created_at, d.word_count, u.key as file_key, u.size, u.extension, $CASE_EXPR as source_kb from documents d join datasets ds on ds.id=d.dataset_id left join upload_files u on u.id=((d.data_source_info::jsonb->>'upload_file_id')::uuid) where d.dataset_id in ($IN_LIST) order by ds.id, d.created_at) to stdout csv header" > "$STAGE/manifest.csv"
MRows=$(wc -l < "$STAGE/manifest.csv")
echo "manifest 行数（含表头）= $MRows"

echo "[2/3] keys（去重）"
$PSQL -t -A -c "select distinct u.key from documents d join upload_files u on u.id=((d.data_source_info::jsonb->>'upload_file_id')::uuid) where d.dataset_id in ($IN_LIST) and u.key is not null" | LC_ALL=C sort -u > "$STAGE/keys.txt"
KRows=$(wc -l < "$STAGE/keys.txt")
echo "keys 行数 = $KRows"

echo "[3/3] 文件子集（从 full-export 本地盘对拷）"
rsync -a --files-from="$STAGE/keys.txt" "$FULL/files/" "$STAGE/files/"
du -sh "$STAGE/files"

# 对账：manifest 无文件档（file_key 为空，如 create-by-text）数
NOFILE=$($PSQL -t -A -c "select count(*) from documents d left join upload_files u on u.id=((d.data_source_info::jsonb->>'upload_file_id')::uuid) where d.dataset_id in ($IN_LIST) and u.key is null")
echo "对账: docs=$((MRows-1)) keys=$KRows 无文件档=$NOFILE （docs-无文件档 应≈keys）"
echo "完成 -> $STAGE"

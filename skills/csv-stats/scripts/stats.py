#!/usr/bin/env python3
"""csv-stats：输出 CSV 的基础统计，JSON 到 stdout。仅用标准库。"""
import csv, json, sys
from collections import Counter

def col_stats(values):
    present = [v for v in values if v != '']
    s = {"missing": len(values) - len(present), "unique": len(set(present))}
    nums = []
    for v in present:
        try:
            nums.append(float(v))
        except ValueError:
            pass
    if present and len(nums) / len(present) >= 0.9:
        s.update(min=min(nums), max=max(nums), mean=round(sum(nums) / len(nums), 4))
    elif present:
        s["top"] = Counter(present).most_common(3)
    return s

def main(path):
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print("无法读取 %s: %s" % (path, e), file=sys.stderr)
        sys.exit(2)
    if not rows:
        json.dump({"rows": 0, "columns": {}}, sys.stdout, ensure_ascii=False)
        return
    cols = {name: col_stats([r.get(name, '') for r in rows]) for name in rows[0]}
    json.dump({"rows": len(rows), "columns": cols}, sys.stdout, ensure_ascii=False, indent=1)

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("用法: stats.py <csv路径>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

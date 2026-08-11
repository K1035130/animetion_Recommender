"""构建清洗后的 tag 词表。

顺序很重要：
  1. 先同义合并（在单部作品内去重，避免同时打了「漫画改」和「漫改」被算两次）
  2. 再算 doc_freq
  3. 再按类别分流
  4. 最后频次截断
反过来做的话，「漫改」这种同义变体会因为单独频次低而被误删。

输出 data/interim/tag_vocab.json
"""

import collections
import json
import pathlib
import sys

import orjson

sys.path.insert(0, "src")
from tag_rules import classify, normalize  # noqa: E402

DUMP = "data/raw/dump/subject.jsonlines"
OUT = pathlib.Path("data/interim/tag_vocab.json")
MIN_DF = 8
FORMS = {"TV", "WEB", "剧场版", "OVA"}

df_after_merge = collections.Counter()
raw_names = collections.defaultdict(set)  # 标准名 -> 合并进来的原始写法
n_anime = 0

with open(DUMP, "rb") as f:
    for line in f:
        r = orjson.loads(line)
        if r["type"] != 2:
            continue
        d = r.get("date") or ""
        y = int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None
        if not y or y < 2011:
            continue
        if not (set(r.get("meta_tags") or []) & FORMS):
            continue
        if (r.get("favorite") or {}).get("done", 0) < 50:
            continue
        n_anime += 1
        # 步骤 1：合并后在本作品内去重
        merged = set()
        for t in r.get("tags") or []:
            std = normalize(t["name"])
            merged.add(std)
            if std != t["name"]:
                raw_names[std].add(t["name"])
        for std in merged:
            df_after_merge[std] += 1

print(f"候选集 {n_anime:,} 部")
print(f"合并后不同 tag: {len(df_after_merge):,}\n")

# 步骤 3+4：分类 + 截断
by_cat = collections.defaultdict(list)
for name, dfq in df_after_merge.items():
    if dfq < MIN_DF:
        continue
    by_cat[classify(name)].append((name, dfq))
for c in by_cat:
    by_cat[c].sort(key=lambda x: -x[1])

total_kept = sum(len(v) for v in by_cat.values())
print(f"=== doc_freq >= {MIN_DF} 的 {total_kept:,} 个 tag 分类结果 ===")
for cat in ("KEEP", "STUDIO", "STAFF", "YEAR", "IP", "FORM", "REGION", "META"):
    items = by_cat.get(cat, [])
    note = {
        "KEEP": "→ 进 tag 向量",
        "STUDIO": "→ 分流，作 AniList studios 交叉验证",
        "STAFF": "→ 分流，作 AniList staff 交叉验证",
    }.get(cat, "→ 丢弃")
    print(f"  {cat:7} {len(items):>4} 个   {note}")

keep = by_cat["KEEP"]
print(f"\n=== 保留的 {len(keep)} 个题材 tag（目标区间 300–800）===")
for i in range(0, len(keep), 100):
    seg = keep[i : i + 100]
    print(f"\n[{i+1}-{i+len(seg)}] df {seg[0][1]}~{seg[-1][1]}")
    print("  " + " | ".join(f"{n}({c})" for n, c in seg))

print("\n=== 同义合并生效情况 ===")
for std, olds in sorted(raw_names.items(), key=lambda kv: -df_after_merge[kv[0]])[:15]:
    print(f"  {std}({df_after_merge[std]})  ← {', '.join(sorted(olds))}")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(
    json.dumps(
        {
            "min_df": MIN_DF,
            "n_anime": n_anime,
            "keep": [n for n, _ in keep],
            "keep_df": dict(keep),
            "diverted": {
                "studio": [n for n, _ in by_cat.get("STUDIO", [])],
                "staff": [n for n, _ in by_cat.get("STAFF", [])],
            },
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print(f"\n已写出 {OUT}")

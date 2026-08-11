"""一次遍历 subject.jsonlines，摸清字段语义。

只读不写。目的是回答建表前必须确认的四件事：
1. type 整数怎么对应 动画/书籍/游戏
2. date 字段的格式有多干净（空值、残缺格式占比）
3. 动画条目的 meta_tags 有没有值（有的话可省掉大量 tag 清洗）
4. platform 整数在动画下的分布（区分 TV/OVA/剧场版）
"""

import collections
import re

import orjson

DUMP = "data/raw/dump/subject.jsonlines"

FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

type_counter = collections.Counter()
# 每个 type 存一条样本，用来人工确认语义
type_sample: dict[int, dict] = {}

date_shape = collections.Counter()
platform_by_type: dict[int, collections.Counter] = collections.defaultdict(
    collections.Counter
)
meta_tag_counter: dict[int, collections.Counter] = collections.defaultdict(
    collections.Counter
)
n_meta_empty: dict[int, int] = collections.Counter()

total = 0
with open(DUMP, "rb") as f:
    for line in f:
        rec = orjson.loads(line)
        total += 1
        t = rec["type"]
        type_counter[t] += 1
        if t not in type_sample:
            type_sample[t] = rec

        platform_by_type[t][rec.get("platform")] += 1

        d = rec.get("date") or ""
        if not d:
            date_shape[f"type{t}:空"] += 1
        elif FULL_DATE.match(d):
            date_shape[f"type{t}:完整YYYY-MM-DD"] += 1
        else:
            date_shape[f"type{t}:其他<{d[:12]}>"] += 1

        metas = rec.get("meta_tags") or []
        if metas:
            for m in metas:
                meta_tag_counter[t][m] += 1
        else:
            n_meta_empty[t] += 1

print(f"总条目数: {total:,}\n")

print("=== type 分布 + 样本 ===")
for t, c in type_counter.most_common():
    s = type_sample[t]
    print(f"type={t:2}  {c:>8,} 条   样本: {s.get('name_cn') or s.get('name')}")
    ib = (s.get("infobox") or "")[:60].replace("\r\n", " ")
    print(f"{'':14}infobox 模板头: {ib}")

print("\n=== date 格式（仅列 top 15）===")
for k, c in date_shape.most_common(15):
    print(f"{k:40} {c:>8,}")

print("\n=== meta_tags 覆盖率 ===")
for t, c in type_counter.most_common():
    empty = n_meta_empty[t]
    print(f"type={t:2}  有值 {c - empty:>7,} / {c:>7,}  ({(c-empty)/c:.1%})")

print("\n=== 各 type 的 meta_tags top 20 ===")
for t in sorted(meta_tag_counter):
    top = meta_tag_counter[t].most_common(20)
    print(f"\ntype={t}: " + ", ".join(f"{n}({c})" for n, c in top))

print("\n=== platform 分布（各 type top 10）===")
for t in sorted(platform_by_type):
    top = platform_by_type[t].most_common(10)
    print(f"type={t}: " + ", ".join(f"{p}:{c:,}" for p, c in top))

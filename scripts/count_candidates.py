"""对 type=2 试算不同筛选条件下的候选集规模。

只统计不落库。目的是在建表前校准存储预算，并决定候选集口径。

⚠️ 【历史快照，2026-08-10】本脚本内的筛选条件是**当时的试算参数**（含
   已废弃的「年份 >= 2011」下限），不是当前生效口径。它的作用是记录
   「各种口径分别有多少部」这个对照过程 —— CLAUDE.md 第 13 节动作 2
   的那张逐层收紧表就是它产出的。

   **当前生效口径在 src/candidates.py，以那里为准。** 现在重跑本脚本
   会得到 2011 切线下的旧数字（7,112 那一版），属预期行为，不要拿它的
   输出去更新文档。
"""

import collections

import orjson

DUMP = "data/raw/dump/subject.jsonlines"

animes = []
with open(DUMP, "rb") as f:
    for line in f:
        rec = orjson.loads(line)
        if rec["type"] != 2:
            continue
        d = rec.get("date") or ""
        year = int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None
        metas = set(rec.get("meta_tags") or [])
        animes.append(
            {
                "id": rec["id"],
                "year": year,
                "metas": metas,
                "score": rec.get("score") or 0.0,
                "done": (rec.get("favorite") or {}).get("done", 0),
                "n_tags": len(rec.get("tags") or []),
            }
        )

print(f"type=2 总计: {len(animes):,}\n")

FORMS = ["TV", "WEB", "剧场版", "OVA", "短片"]
REGIONS = ["日本", "中国", "欧美", "美国", "韩国"]


def show(label, rows):
    if not rows:
        print(f"{label:52} 0")
        return
    n_done0 = sum(1 for r in rows if r["done"] == 0)
    n_notag = sum(1 for r in rows if r["n_tags"] == 0)
    n_noscore = sum(1 for r in rows if r["score"] == 0)
    print(
        f"{label:52} {len(rows):>6,}   "
        f"无人看过 {n_done0:>5,} | 无tag {n_notag:>5,} | 无评分 {n_noscore:>5,}"
    )


y2011 = [a for a in animes if a["year"] and a["year"] >= 2011]
print("=== 逐层收紧 ===")
show("type=2 全部", animes)
show("+ 有放送年份", [a for a in animes if a["year"]])
show("+ 年份 >= 2011", y2011)

print("\n--- 在 [年份>=2011] 基础上按形态筛 ---")
for f in FORMS:
    show(f"  形态 = {f}", [a for a in y2011 if f in a["metas"]])
show("  形态 ∈ {TV, WEB}", [a for a in y2011 if a["metas"] & {"TV", "WEB"}])
show(
    "  形态 ∈ {TV, WEB, 剧场版, OVA}",
    [a for a in y2011 if a["metas"] & {"TV", "WEB", "剧场版", "OVA"}],
)

print("\n--- 在 [年份>=2011] 基础上按地区筛 ---")
for r in REGIONS:
    show(f"  地区 = {r}", [a for a in y2011 if r in a["metas"]])

print("\n--- 组合方案 ---")
tvweb = [a for a in y2011 if a["metas"] & {"TV", "WEB"}]
show("A. 2011+ / TV+WEB / 不限地区", tvweb)
show("B. 2011+ / TV+WEB / 仅日本", [a for a in tvweb if "日本" in a["metas"]])
show("C. 2011+ / TV+WEB / 有人看过(done>0)", [a for a in tvweb if a["done"] > 0])
show(
    "D. 2011+ / TV+WEB / done>=50",
    [a for a in tvweb if a["done"] >= 50],
)
wide = [a for a in y2011 if a["metas"] & {"TV", "WEB", "剧场版", "OVA"}]
show("E. 2011+ / TV+WEB+剧场版+OVA / done>=50", [a for a in wide if a["done"] >= 50])

print("\n=== 年份分布（TV+WEB, done>0）===")
yc = collections.Counter(a["year"] for a in tvweb if a["done"] > 0)
for y in sorted(yc):
    print(f"  {y}: {yc[y]:>4,}", end="\n" if (y - 2010) % 4 == 0 else "")
print()

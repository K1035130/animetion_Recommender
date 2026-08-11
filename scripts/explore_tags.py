"""摸清候选集 7,112 部的用户 tag 分布，为清洗策略提供依据。

只统计不清洗。要回答的问题：
1. 一共多少个不同 tag？长尾有多长？
2. 「文档频次 < 20 丢弃」这个阈值能留下多少个 tag？目标是 300–800
3. 噪声（年份/季度/元评价/形态/地区）占多大比例？
"""

import collections
import re

import orjson

DUMP = "data/raw/dump/subject.jsonlines"
FORMS = {"TV", "WEB", "剧场版", "OVA"}

# doc_freq: tag 出现在多少部作品里
doc_freq = collections.Counter()
# total_count: tag 在全候选集上的累计投票数
total_count = collections.Counter()
n_anime = 0
tags_per_anime = []

with open(DUMP, "rb") as f:
    for line in f:
        rec = orjson.loads(line)
        if rec["type"] != 2:
            continue
        d = rec.get("date") or ""
        year = int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None
        if not year or year < 2011:
            continue
        if not (set(rec.get("meta_tags") or []) & FORMS):
            continue
        if (rec.get("favorite") or {}).get("done", 0) < 50:
            continue

        n_anime += 1
        tags = rec.get("tags") or []
        tags_per_anime.append(len(tags))
        for t in tags:
            doc_freq[t["name"]] += 1
            total_count[t["name"]] += t["count"]

print(f"候选集: {n_anime:,} 部")
print(f"不同 tag 总数: {len(doc_freq):,}")
print(
    f"每部平均 tag 数: {sum(tags_per_anime)/len(tags_per_anime):.1f} "
    f"(中位 {sorted(tags_per_anime)[len(tags_per_anime)//2]})"
)

print("\n=== 按 doc_freq 阈值截断后剩余 tag 数 ===")
for th in (2, 5, 10, 20, 30, 50, 100, 200):
    n = sum(1 for v in doc_freq.values() if v >= th)
    print(f"  出现在 >= {th:>3} 部作品中: {n:>6,} 个 tag")

print("\n=== top 60 tag（doc_freq 排序）===")
for i, (name, df) in enumerate(doc_freq.most_common(60), 1):
    print(f"{i:3}. {name:<16} 作品数={df:<5} 累计票={total_count[name]:,}")
    if i % 20 == 0:
        print()

# --- 噪声模式识别 ---
PATTERNS = {
    "年份/季度": re.compile(r"(^\d{4}$|^\d{4}年|\d{1,2}月新?番|^[春夏秋冬]季?$|^\d{4}-\d{1,2})"),
    "形态": re.compile(r"^(TV|OVA|OAD|WEB|剧场版|电影|动画|番剧|短片|特别篇|SP|PV|MV)$", re.I),
    "元评价": re.compile(r"^(神作|经典|好评|催泪|治愈|致郁|补番|想看|在看|看过|力荐|烂片|雷|坑|未完|完结|补|MARK|mark|标记|待看|收藏)$"),
    "载体/来源": re.compile(r"^(漫画改|小说改|游戏改|轻改|原创|改编|续作|续集|第[一二三四五六七八九十\d]+季)$"),
    "地区/公司": re.compile(r"^(日本|中国|美国|欧美|韩国|国产|国创|日常番)$"),
}

print("\n=== 噪声模式在 doc_freq>=20 的 tag 里占比 ===")
kept = {k: v for k, v in doc_freq.items() if v >= 20}
matched = set()
for label, pat in PATTERNS.items():
    hits = [k for k in kept if pat.search(k)]
    matched |= set(hits)
    sample = ", ".join(sorted(hits, key=lambda x: -kept[x])[:12])
    print(f"\n[{label}] {len(hits)} 个")
    print(f"    {sample}")

print(f"\ndoc_freq>=20 共 {len(kept):,} 个，正则可识别噪声 {len(matched):,} 个，"
      f"剩余 {len(kept)-len(matched):,} 个")

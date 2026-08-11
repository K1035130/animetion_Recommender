"""候选集口径 —— 唯一事实来源。

所有脚本（探查、词表构建、ETL）都必须从这里取筛选逻辑，
不要各自复制一份 —— 口径一改就会静默漂移。

口径演进：
  2026-08-10  初版：2011+ / TV·WEB / done>=50            → 4,689 部
  2026-08-10  加剧场版+OVA（扩大用户可打分范围）           → 7,112 部
  2026-08-10  取消年份下限（支持「经典回顾」推荐模式）      → 11,259 部
"""

from typing import Any, Iterator

import orjson

DUMP = "data/raw/dump/subject.jsonlines"

TYPE_ANIME = 2

# 形态：排除「短片」和无形态标签的同人/MV/概念影像。
# 实测无形态标签的 done>=50 条目多为同人动画和 MV，不是商业作品。
FORMS = {"TV", "WEB", "剧场版", "OVA"}

# 质量门槛。实测这个阈值同时把「无 tag」「无评分」「无人看过」三个
# 数据质量问题清零 —— 有 50 人标记看过的条目必然已积累 tag 和评分。
MIN_DONE = 50

# 年份下限：None = 不设限。
# 「经典回顾」模式需要 2011 年前的作品（狼与香辛料 2008、EVA 1995）。
MIN_YEAR: int | None = None


def parse_year(rec: dict[str, Any]) -> int | None:
    """dump 的 date 要么是完整 YYYY-MM-DD 要么是空，无残缺格式（已实测）。"""
    d = rec.get("date") or ""
    if len(d) >= 4 and d[:4].isdigit():
        return int(d[:4])
    return None


def is_candidate(rec: dict[str, Any]) -> bool:
    if rec.get("type") != TYPE_ANIME:
        return False
    year = parse_year(rec)
    if year is None:
        return False
    if MIN_YEAR is not None and year < MIN_YEAR:
        return False
    if not (set(rec.get("meta_tags") or []) & FORMS):
        return False
    return (rec.get("favorite") or {}).get("done", 0) >= MIN_DONE


def iter_candidates(path: str = DUMP) -> Iterator[dict[str, Any]]:
    """流式产出候选集条目。946 MB 文件，不要一次性读进内存。"""
    with open(path, "rb") as f:
        for line in f:
            rec = orjson.loads(line)
            if is_candidate(rec):
                yield rec


def form_of(rec: dict[str, Any]) -> str | None:
    """取形态标签。一部作品可能同时有多个（如剧场版总集篇），按优先级取一个。"""
    metas = set(rec.get("meta_tags") or [])
    for f in ("TV", "WEB", "剧场版", "OVA"):
        if f in metas:
            return f
    return None

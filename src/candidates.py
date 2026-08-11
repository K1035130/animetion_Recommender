"""候选集口径 —— 唯一事实来源。

所有脚本（探查、词表构建、ETL）都必须从这里取筛选逻辑，
不要各自复制一份 —— 口径一改就会静默漂移。

口径演进：
  2026-08-10  初版：2011+ / TV·WEB / done>=50            → 4,689 部
  2026-08-10  加剧场版+OVA（扩大用户可打分范围）           → 7,112 部
  2026-08-10  取消年份下限（支持「经典回顾」推荐模式）      → 11,259 部
  2026-08-10  date 为空时回退到 infobox（救回国产老动画）    → 11,453 部
"""

import re
from collections.abc import Iterator
from typing import Any

import bgm_tv_wiki
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


# infobox 里放日期的字段名（Bangumi 老条目常只填这里，不填顶层 date）
_DATE_KEYS = (
    "放送开始", "放送开始日", "上映年度", "上映日期", "发行日期",
    "首播", "公开日", "发售日",
)
_YEAR_RE = re.compile(r"(19\d{2}|20\d{2})")


def parse_year(rec: dict[str, Any]) -> int | None:
    """取放送年份。

    顶层 `date` 优先（格式已实测：要么完整 YYYY-MM-DD 要么为空，无残缺格式）。
    date 为空时回退到 infobox —— **这一步不能省**：213 部满足其余全部条件的
    条目没有 date，其中 207 部（97%）能从 infobox 救回，而且几乎全是
    上海美术电影制片厂的国产经典（大闹天宫 done=9,935、葫芦兄弟、黑猫警长、
    天书奇谭）。这批正是「经典回顾」模式的核心内容，丢了等于功能残废。
    """
    d = rec.get("date") or ""
    if len(d) >= 4 and d[:4].isdigit():
        return int(d[:4])
    return _year_from_infobox(rec.get("infobox") or "")


def _year_from_infobox(infobox: str) -> int | None:
    """用官方 parser 读 infobox，不自己写 wiki 语法正则。"""
    if not infobox:
        return None
    try:
        wiki = bgm_tv_wiki.parse(infobox)
    except bgm_tv_wiki.WikiSyntaxError:
        return None
    for field in wiki.fields:
        if field.key not in _DATE_KEYS:
            continue
        # value 可能是 str / None / tuple[Item]，只处理字符串形式
        if not isinstance(field.value, str):
            continue
        m = _YEAR_RE.search(field.value)
        if m:
            return int(m.group(1))
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

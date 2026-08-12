"""问卷选题 v0：纯热度 + 续作折叠。

第 9 节的完整方案（聚类选代表 + 信息增益动态选题）排在第 5–6 周。
v0 先用纯热度，但**必须处理续作**，否则问卷质量会明显受损。

用户的作答选项（2026-08-11 定）：
    看过 → 1–10 分
    没看过但想尝试 → WISH_SCORE
    没看过也不感兴趣 → PASS_SCORE
    （不作答 → 不产生记录，用缺失表示，见第 4 节 user_rating 的设计）

⚠️ 未观看的两个选项**必须带较低置信度**，不能和真实评分等权。
   实测算例：12 个不感兴趣(3) + 5 个想尝试(8) + 8 个真实评分(均 7.5)，
   收缩后 μ=5.56，「想尝试」权重 +2.44 反而高于真实评分的 +1.94 ——
   「我想看看」压过「我看完了觉得不错」，显然反了。
   「想尝试」只是基于封面/题材的预期；「不感兴趣」噪声更大（可能只是
   没听说过或封面不合眼缘），所以两者的置信度还应当不对称。
   系数第 5 周有 baseline 后再调，现在做成可配置。
"""

import datetime
from dataclasses import dataclass

import psycopg

from src import series

# 未观看选项的赋分与置信度
WISH_SCORE = 8.0
PASS_SCORE = 3.0
WISH_CONFIDENCE = 0.5      # 相对真实评分的权重
PASS_CONFIDENCE = 0.3      # 更低 —— 「没兴趣」的噪声大于「想试试」

# 问卷候选池：第 9 节要求限制在 TV+WEB。剧场版/OVA 大量是系列续作，
# 用户没看过前作就只能选「没看过」。
POOL_FORMS = ("TV", "WEB")

# 观众资历 → 出题的年份回溯窗口。
# 问 3 年经验的观众「你看过灼眼的夏娜吗」，拿到的必然是「没看过」，
# 而那比一条真实评分的信息量低得多 —— 等于浪费一题。
#
# ⚠️ **资历只影响出题范围，不影响推荐范围。**
#    新观众照样可能爱上老番，那正是第 7 节「经典回顾」模式存在的意义。
#    这和第 7 节「模式只作用在候选池过滤，不影响偏好学习」是同一条原则的两面。
#
# 实测三档折叠去重后各有 1,493 / 2,518 / 4,625 部可选，题目绰绰有余；
# 且三档 Top30 的题材分布高度一致（都是漫画改/奇幻/搞笑/日常/战斗主导），
# 不存在「限制年份会欠采样老题材」的问题。
EXPERIENCE: dict[str, int | None] = {
    "new": 5,        # 入坑 ~3 年 → 问最近 5 年
    "mid": 10,       # ~5 年     → 最近 10 年
    "veteran": None,  # 老资历    → 不限
}
EXPERIENCE_LABEL = {
    "new": "新观众（3 年左右）—— 只问最近 5 年的番",
    "mid": "有点资历（5 年左右）—— 问最近 10 年的番",
    "veteran": "老观众 —— 不限年份",
}


@dataclass
class Item:
    subject_id: int
    name: str
    year: int | None
    done: int
    form: str | None
    replaced_from: int | None = None    # 若由某部续作折叠而来，记下原始 id


def select_items(conn: psycopg.Connection, n: int = 30, *,
                 include_nsfw: bool = False,
                 fold_sequels: bool = True,
                 experience: str = "veteran") -> list[Item]:
    """按热度选 n 部，续作折叠成系列第一部。

    折叠而非丢弃：系列第一部通常热度更高、更适合当问卷题目
    （问「你看过进击的巨人吗」远比问「你看过进击的巨人第三季Part.2吗」有效）。

    ⚠️ 根节点不限形态。候选池限制在 TV+WEB 的理由是「剧场版/OVA 多为续作」，
       而根节点按定义就不是续作，这条理由对它不成立 —— 攻壳机动队(1995 剧场版)
       是整个系列的入口，完全适合作为问卷题目。
    """
    if experience not in EXPERIENCE:
        raise ValueError(f"未知资历 {experience!r}，可选 {list(EXPERIENCE)}")
    back = EXPERIENCE[experience]
    floor = (datetime.datetime.now(datetime.UTC).year - back) if back else None

    smap = series.load() if fold_sequels else {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, COALESCE(name_cn, name), air_year, fav_done, form,
                   nsfw
            FROM anime_profile
            ORDER BY fav_done DESC
        """)
        rows = cur.fetchall()
    meta = {r[0]: r for r in rows}

    picked: dict[int, Item] = {}
    for sid, name, year, done, form, nsfw in rows:
        if not include_nsfw and nsfw:
            continue
        if form not in POOL_FORMS:
            continue
        root = smap.get(sid, sid)
        if root in picked:
            continue
        r = meta.get(root)
        if r is None:                      # 根不在候选集里（极少），退回本作
            root, r = sid, (sid, name, year, done, form, nsfw)
        if not include_nsfw and r[5]:      # 根是 nsfw 则整条系列跳过
            continue
        # ⚠️ 年份卡在**根节点**上，不是原作品上。折叠后展示的是根，
        #    一部 2023 年的续作若根在 2015 年，对新观众来说仍是「没看过」——
        #    问「无职转生 第三季」而他没看过第一季，照样白问一题。
        if floor is not None and (r[2] is None or r[2] < floor):
            continue
        picked[root] = Item(subject_id=root, name=r[1], year=r[2], done=r[3],
                            form=r[4],
                            replaced_from=sid if root != sid else None)

    return sorted(picked.values(), key=lambda x: -x.done)[:n]


def to_rating(choice: str, score: float | None = None
              ) -> tuple[float, float, bool] | None:
    """把作答映射成 (分数, 置信度, 是否从推荐结果剔除)。

    返回 None 表示不产生记录 —— 「跳过」用缺失表示，不用 score=0 占位
    （第 4 节 user_rating 的设计）。

    choice ∈ {'seen', 'wish', 'pass', 'skip'}

    ⚠️ 第三项对 'wish' 是 **False**：用户说「想尝试」的作品应当**留在**
       推荐结果里 —— 那正是他要的。只有「看过」和「不感兴趣」才剔除。
    """
    if choice == "seen":
        if score is None:
            raise ValueError("choice='seen' 必须带分数")
        return float(score), 1.0, True
    if choice == "wish":
        return WISH_SCORE, WISH_CONFIDENCE, False
    if choice == "pass":
        return PASS_SCORE, PASS_CONFIDENCE, True
    return None

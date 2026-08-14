"""问卷选题：MMR 多样性序 + 续作折叠 + 跳过已评分（支持多次作答）。

⚠️ **2026-08-14 从「纯热度」改为 MMR。** 纯热度选出的 30 题两两余弦均值
   0.4552，比随机抽 30 部（0.3627）**还冗余** —— 热门作品扎堆在校园/恋爱/日常，
   问 30 题拿不到 30 题的信息量。MMR 把这个数压到 0.3781 而中位热度几乎不掉
   （44,146 → 41,040）。原计划的 k-means 聚类被实测否掉，详见
   sql/005_mmr_rank.sql 与 CLAUDE.md 第 9 节顶部的标注。

位次由 scripts/build_clusters.py 离线算好写进 `anime_profile.mmr_rank`。
⚠️ 必须离线算：MMR 要拉全池 4,439 条向量做 N 轮矩阵乘，正是 serverless 禁止的事。

第 9 节的信息增益动态选题排在第 6 周 —— 它与本模块**串联不替代**：
MMR 序充当候选池，IG 在池内动态挑下一题。

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
from collections.abc import Collection
from dataclasses import dataclass

import psycopg

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
                 experience: str = "veteran",
                 exclude: Collection[int] = ()) -> list[Item]:
    """按 mmr_rank（多样性序）选 n 部，续作折叠成系列第一部。

    折叠而非丢弃：系列第一部通常热度更高、更适合当问卷题目
    （问「你看过进击的巨人吗」远比问「你看过进击的巨人第三季Part.2吗」有效）。

    ⚠️ 根节点不限形态。候选池限制在 TV+WEB 的理由是「剧场版/OVA 多为续作」，
       而根节点按定义就不是续作，这条理由对它不成立 —— 攻壳机动队(1995 剧场版)
       是整个系列的入口，完全适合作为问卷题目。

    `exclude` —— 已有评分记录的 subject_id，用于**多次作答**：答过一轮之后
    再答，跳过已评分的，继续往下取题。

    ⚠️ **这个参数保持了第 2 节的架构铁律**：调用方传入，函数不关心它来自
       游客的 localStorage 还是注册用户的 `user_rating` 表。与
       `score(catalog, ratings, ...)` 同一条纪律 —— 第 6 周加账号只是换数据源。

    ⚠️ **判据是「该条目自己有没有评分」，不在系列内传递。**
       用户给《JoJo 第三部》打过分，不代表看过第一部 —— 这类每季可独立观看的
       作品跳着看很常见。问卷的目的是扩充用户的资料库，只要根节点本身没评分
       就该问。（按系列传递会静默少掉大量可问的题。）

    ⚠️ **「没看过」(skip) 不进 exclude。** `to_rating()` 对 skip 返回 None、
       不产生评分记录，所以它天然不在排除集里、下轮会再出现 —— 这是有意的：
       用户当时没看过，过一阵可能就看了。**不需要为此再维护一个「已作答」集合。**

    💡 多次作答之所以成立，是因为 `mmr_rank` 存的是**全池 4,439 条的完整贪心序**
       而不是只有前 30。MMR 的第 31 位本就是「已选前 30 位的前提下信息增量最大的
       那一部」，所以第二轮天然就是「补充第一轮之外的信息」，不是随便往下顺延。
       实测热度衰减很平缓：第 1 轮中位 41,040，第 6 轮仍有 18,462。
    """
    if experience not in EXPERIENCE:
        raise ValueError(f"未知资历 {experience!r}，可选 {list(EXPERIENCE)}")
    back = EXPERIENCE[experience]
    floor = (datetime.datetime.now(datetime.UTC).year - back) if back else None

    # ⚠️ 系列关系读 **anime_profile.series_root 列**，不读 series_root.json。
    #    那个文件不入 git（data/interim/* 被忽略，只放行 tag_vocab.json），
    #    线上根本不存在 —— 而 series.load() 默认 required=True 会抛
    #    FileNotFoundError，`/questionnaire` 直接 500。
    #    列由 scripts/build_tag_vectors.py 写入，值与 series.load() 逐条相同。
    #    COALESCE 兜底：列没回填时退化成「不折叠」，与 fold_sequels=False 同义。
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, COALESCE(name_cn, name), air_year, fav_done, form,
                   nsfw, COALESCE(series_root, subject_id), mmr_rank
            FROM anime_profile
            ORDER BY fav_done DESC
        """)
        rows = cur.fetchall()
    meta = {r[0]: r for r in rows}
    # 位次由 scripts/build_clusters.py 离线算好（MMR 贪心序）。
    # ⚠️ 这里仍按 fav_done 取全表：折叠需要遍历所有作品才能把续作映射到根，
    #    最终排序才换成 mmr_rank。
    rank_of = {r[0]: r[7] for r in rows if r[7] is not None}

    skip = frozenset(exclude)
    picked: dict[int, Item] = {}
    for sid, name, year, done, form, nsfw, sroot, _rank in rows:
        if not include_nsfw and nsfw:
            continue
        if form not in POOL_FORMS:
            continue
        root = sroot if fold_sequels else sid
        if root in picked or root in skip:
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

    # ⚠️ **按 mmr_rank 排，不再按热度。**（2026-08-14 改，见 sql/005_mmr_rank.sql）
    #    纯热度选出来的题目彼此高度相似 —— 实测 30 题两两余弦均值 0.4552，
    #    比随机抽 30 部（0.3627）还冗余，因为热门作品扎堆在校园/恋爱/日常。
    #    问 30 题拿不到 30 题的信息量。MMR 序把这个数压到 0.3781 且热度几乎不掉。
    #
    # ⚠️ 没有位次的根排在最后、退回热度序 —— 池外的根（非 TV/WEB、
    #    nsfw、或 vec 为空）本来就不该优先出题，但也不能凭空消失，
    #    否则 include_nsfw=True 会直接少掉一批题。
    def order(it: Item) -> tuple[int, int, int]:
        r = rank_of.get(it.subject_id)
        return (0, r, 0) if r is not None else (1, 0, -it.done)

    return sorted(picked.values(), key=order)[:n]


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

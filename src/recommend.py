"""P0 推荐：tag 向量余弦 + mean-centered 打分。

⚠️ **架构铁律（第 2 节）：打分接口是无状态的 —— 评分随请求传入。**
    `score()` 只接受一组 (subject_id, rating)，不关心它来自游客的
    localStorage 还是注册用户的 user_rating 表。第 6 周加账号系统时
    只是多一个数据来源，不用回头重写推荐链路，游客与注册用户也永远
    走同一条代码路径（少一半 bug，评测时也不会出现两套口径）。

维度空间 = 清洗后的题材 tag 词表（见 data/interim/tag_vocab.json）。
用户 tag 与官方 meta_tags 落进**同一个**空间：meta_tags 同样过
tag_rules 的 normalize()+classify()，形态/地区会被自动分流掉。
这一点对 139 部「清洗后 tags 为空」的作品是必需的 —— 其中包括
化物语(done=37,573)，它的 11 个用户 tag 全是 staff/CV/年份，
只有 meta_tags 里的「奇幻」「小说改」能给它一个向量。
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import psycopg

from src import tag_rules
from src.textproc import keep_tags

Weighting = Literal["logtf-idf", "binary"]
Mode = Literal["all", "recent", "classic"]

CLASSIC_BEFORE = 2011      # 第 7 节「经典回顾」的分界线

# mean-centering 的先验。取值不是拍脑袋 —— 由库内 score_details 直方图
# 逐票统计得出：全站 3,900 万张选票的均分是 7.074，即「典型用户给典型作品
# 打多少分」。按作品平均是 6.30，但那会被大量冷门低分作品拉低，不是用户视角。
PRIOR_MEAN = 7.07
# 先验相当于多少条虚拟评分。2 条时：单条 10 分 → μ=8.05，权重 +1.95（能出推荐）；
# 单条 3 分 → μ=5.71，权重 -2.71（推荐不相似的，也合理）。
# 评分攒到 10 条以上时先验的影响已可忽略，用户自己的均值重新主导。
PRIOR_WEIGHT = 2.0


@dataclass
class Catalog:
    """全库的 tag 矩阵。一次构建，多次打分。"""

    ids: np.ndarray            # (n,) subject_id
    vocab: list[str]           # (d,) 维度名，供解释推荐理由用
    mat: np.ndarray            # (n, d) float32，**行已 L2 归一化**
    year: np.ndarray           # (n,) air_year
    nsfw: np.ndarray           # (n,) bool
    done: np.ndarray           # (n,) fav_done，热度兜底与多样性用
    name: list[str]            # (n,) 展示名

    def index_of(self, subject_id: int) -> int | None:
        hit = np.flatnonzero(self.ids == subject_id)
        return int(hit[0]) if len(hit) else None


def _clean(names, counts, vocab: frozenset[str]) -> dict[str, float]:
    """把一组原始 tag 名归一化、分流、并到词表维度上。"""
    out: dict[str, float] = {}
    for nm, ct in zip(names, counts, strict=True):
        canon = tag_rules.normalize(nm)
        if canon not in vocab or tag_rules.classify(canon) != "KEEP":
            continue
        out[canon] = out.get(canon, 0.0) + float(ct)
    return out


def build_catalog(conn: psycopg.Connection,
                  weighting: Weighting = "logtf-idf") -> Catalog:
    """从库里拉出全部作品，构建 (n, d) 的 tag 矩阵。

    权重：log(1+count) * idf，再按行 L2 归一化。
      · log —— count 随作品热度缩放（done=50 的作品 tag count 是个位数，
        done=50,000 的是几千），不压缩量级的话热门作品会主导一切
      · idf —— 「漫画改」覆盖 3,798 部，几乎不携带口味信息，应当降权
      · L2 —— 余弦的前提；同时抵消「tag 多的作品模长更大」

    binary 模式留给第 5 周做 ablation（第 10 节的可选对照）。
    """
    vocab = keep_tags()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, COALESCE(name_cn, name), air_year, nsfw, fav_done,
                   tags, meta_tags
            FROM anime_profile
            ORDER BY subject_id
        """)
        rows = cur.fetchall()

    per_work: list[dict[str, float]] = []
    ids, years, nsfws, dones, names = [], [], [], [], []
    for sid, nm, yr, nsfw, done, tags, metas in rows:
        tags = tags or []
        w = _clean([t["name"] for t in tags], [t.get("count") or 0 for t in tags],
                   vocab)
        # meta_tags 没有票数。用该作品 tag 票数的中位数 —— 官方标签的可信度
        # 不低于用户 tag，但也不该压过最高票的那个。作品一个 tag 都没有时
        # 退化为 1，此时向量各维等权，L2 归一化后依然可用。
        base = float(np.median(list(w.values()))) if w else 1.0
        for m in _clean(metas or [], [base] * len(metas or []), vocab).items():
            w.setdefault(m[0], m[1])
        per_work.append(w)
        ids.append(sid)
        years.append(yr)
        nsfws.append(nsfw)
        dones.append(done)
        names.append(nm)

    vlist = sorted(vocab)
    col = {t: i for i, t in enumerate(vlist)}
    mat = np.zeros((len(rows), len(vlist)), dtype=np.float32)
    for i, w in enumerate(per_work):
        for t, c in w.items():
            mat[i, col[t]] = 1.0 if weighting == "binary" else np.log1p(c)

    if weighting == "logtf-idf":
        dfreq = (mat > 0).sum(axis=0)
        idf = np.log((len(rows) + 1) / (dfreq + 1)).astype(np.float32)
        mat *= idf

    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0        # 零向量保持为零，不产生 nan
    mat /= norm

    return Catalog(ids=np.array(ids), vocab=vlist, mat=mat,
                   year=np.array([y if y is not None else 0 for y in years]),
                   nsfw=np.array(nsfws, dtype=bool),
                   done=np.array(dones), name=names)


def preference_vector(cat: Catalog, ratings: list[tuple[int, float]],
                      *, prior_mean: float = PRIOR_MEAN,
                      prior_weight: float = PRIOR_WEIGHT) -> np.ndarray:
    """把一组评分压成一个 mean-centered 的偏好向量。

    减去用户自己的均分是关键：有人习惯全打 8–10，有人全打 4–6。
    不减的话前者的每一部都会被当成「喜欢」，学到的其实是打分习惯而非口味。
    减完之后正权重 = 高于此人平均水平，负权重 = 低于 —— 后者同样有信息，
    应当把相似的作品往下压。

    ⚠️ **但纯用户均值在两种情况下会整个退化成零向量**，实测发现：
      · 只评了一部 —— μ 就等于那一部的分，权重恒为 0
      · 所有评分相同（用户全打 8 分）—— 同理
    问卷流程里这是致命的：答完第一题就该有反馈，却会拿到空结果。

    所以 μ 用**向先验收缩**的形式：
        μ = (Σr + k·prior) / (n + k)
    n 小时 μ 靠近 prior（单条 10 分 → 权重为正，能出推荐）；
    n 大时 μ 收敛到用户自己的均值（保住去除打分习惯的本意）。
    这是标准的贝叶斯收缩，不是为了绕过边界情况打的补丁。
    """
    rows, weights = [], []
    scores = [r for _, r in ratings]
    n = len(scores)
    mu = ((sum(scores) + prior_weight * prior_mean) / (n + prior_weight)
          if n else 0.0)
    for sid, r in ratings:
        i = cat.index_of(sid)
        if i is None:
            continue                      # 不在候选集里的作品直接忽略
        rows.append(i)
        weights.append(r - mu)
    if not rows:
        return np.zeros(cat.mat.shape[1], dtype=np.float32)
    w = np.array(weights, dtype=np.float32)[:, None]
    return (cat.mat[rows] * w).sum(axis=0)


def score(cat: Catalog,
          ratings: list[tuple[int, float]],
          *,
          mode: Mode = "all",
          include_nsfw: bool = False,
          top_k: int = 20) -> list[tuple[int, str, float]]:
    """无状态打分。返回 [(subject_id, 展示名, 分数)]，按分数降序。

    分数是偏好向量与作品向量的余弦，范围 [-1, 1]，
    正 = 比此人平均口味更对味。
    """
    pref = preference_vector(cat, ratings)
    n = np.linalg.norm(pref)
    if n == 0:
        return []
    sims = cat.mat @ (pref / n)

    keep = np.ones(len(cat.ids), dtype=bool)
    if not include_nsfw:
        keep &= ~cat.nsfw                          # 第 13 节：入库保留、默认过滤
    if mode == "classic":
        keep &= cat.year < CLASSIC_BEFORE
    elif mode == "recent":
        keep &= cat.year >= CLASSIC_BEFORE
    rated = {sid for sid, _ in ratings}
    keep &= ~np.isin(cat.ids, list(rated))         # 看过的不再推荐

    idx = np.flatnonzero(keep)
    order = idx[np.argsort(-sims[idx])][:top_k]
    return [(int(cat.ids[i]), cat.name[i], float(sims[i])) for i in order]

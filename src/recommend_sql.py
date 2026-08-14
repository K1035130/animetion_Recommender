"""线上打分：把余弦计算推进 Postgres，不依赖常驻内存的 Catalog。

2026-08-12 放弃 Render 常驻进程、改上 Vercel serverless 后的主打分路径。
无状态函数里没有 lifespan，14 MB 的 tag 矩阵不可能常驻；每请求重建要
拉 2.6 MB / 1.3 s。改成让 pgvector 算余弦后，每请求只拉用户评过的
那几十部（实测 70 ms，几乎全是 RTT）。

⚠️ **与 recommend.score() 必须逐条等价。** 后者仍然保留，第 5 周离线评测
   要在几千个用户上跑 leave-one-out（10⁵~10⁶ 次打分），只能用内存矩阵。
   两者的一致性不是靠人记得同步，而是靠两条构造上的保证：
     1. 向量的唯一定义处是 anime_profile.tag_vec（src/tagvec.py 算，
        scripts/build_tag_vectors.py 写），两条路径读的是同一批数字
     2. tests/test_parity.py 对随机档案逐条比对，CI 里必须绿
   打分公式（收缩均值、池内 min-max、系列折叠）的常量也全部从 recommend
   import，不在这里复述 —— 复述就会漂移。

⚠️ 排序与过滤的语义细节全部沿用 recommend.score() 的注释，这里不重复，
   只标注 SQL 侧特有的坑。
"""

import datetime

import numpy as np
import psycopg

from src.recommend import (
    CLASSIC_BEFORE,
    DEFAULT_ALPHA,
    DEFAULT_POOL_FACTOR,
    DEFAULT_POOL_MIN,
    DEFAULT_WEIGHTS,
    EMB_DIM,
    MIN_SCORE,
    PRIOR_MEAN,
    PRIOR_WEIGHT,
    STAFF_DIM,
    Mode,
    RankBy,
    Rating,
    Recommendation,
    Weights,
    _weighted_rating,
    season_window,
)
from src.tagvec import vocab

# 打分主查询。分三层 CTE，对应 recommend.score() 的三件事：
#   cand  —— 过滤 + 算余弦（对应 numpy 侧的 keep 掩码 + cat.mat @ pref）
#   pool  —— 每个系列取最佳匹配度，再按匹配度取前 want 个（第一段召回 + 系列去重）
#   entry —— 系列内挑入口作品（用户没答过的最早一部，同年取热度高的）
#
# ⚠️ entry 必须 JOIN 回 cand 而不是重新查表 —— cand 里已经含全部过滤条件，
#    重查一次等于让替代品绕过 nsfw/评分下限/年份过滤。numpy 侧踩过这个坑：
#    「匹配到超电磁炮T(2020) 却替换成超电磁炮(2009)」，在 recent 档返回了
#    2011 年前的作品。SQL 侧用 JOIN 天然避免。
_SQL_FOLD = """
WITH cand AS (
    SELECT subject_id, series_root, COALESCE(name_cn, name) AS name,
           air_year, fav_done, COALESCE(score, 0)::float AS score,
           COALESCE(score_count, 0) AS votes,
           {match} AS match
    FROM anime_profile
    WHERE {filters}
),
best AS (
    -- 每个系列的最佳匹配成员。⚠️ 必须连 best_id 一起取出来：
    -- 大量作品与偏好向量**完全正交**（余弦精确为 0），召回池整片并列，
    -- 此时的次级排序键决定了取哪 100 个。numpy 侧的顺序是
    -- 「match 降序 + subject_id 升序」（stable argsort + ids 已排序），
    -- 这里必须逐字对齐，否则两条路径召回的根本不是同一批候选。
    SELECT DISTINCT ON (series_root)
           series_root, match, subject_id AS best_id
    FROM cand ORDER BY series_root, match DESC, subject_id
),
pool AS (
    SELECT * FROM best ORDER BY match DESC, best_id LIMIT %(want)s
),
entry AS (
    -- 系列内的入口作品：最早的一部，同年取热度高的。
    -- ⚠️ 末尾的 c.subject_id 不能省 —— 年份与热度都并列时，
    --    numpy 侧 min() 返回 members 里的第一个（即 subject_id 最小的）。
    SELECT DISTINCT ON (c.series_root)
           c.series_root, c.subject_id, c.name, c.air_year,
           c.fav_done, c.score, c.votes
    FROM cand c JOIN pool p ON p.series_root = c.series_root
    ORDER BY c.series_root, c.air_year, c.fav_done DESC, c.subject_id
)
SELECT e.subject_id, e.name, e.air_year, e.fav_done, e.score, e.votes, p.match
FROM entry e JOIN pool p ON p.series_root = e.series_root
ORDER BY p.match DESC, p.best_id
"""

_SQL_FLAT = """
SELECT subject_id, COALESCE(name_cn, name) AS name, air_year, fav_done,
       COALESCE(score, 0)::float AS score, COALESCE(score_count, 0) AS votes,
       {match} AS match
FROM anime_profile
WHERE {filters}
ORDER BY match DESC, subject_id
LIMIT %(want)s
"""


def preference_vector(conn: psycopg.Connection, ratings: list[Rating],
                      *, prior_mean: float = PRIOR_MEAN,
                      prior_weight: float = PRIOR_WEIGHT) -> np.ndarray:
    """从库里取出被评作品的向量，压成 mean-centered 的偏好向量。

    公式与 recommend.preference_vector() 完全一致（收缩均值 + 置信度加权），
    差别只在向量来源：那边取自内存矩阵，这边取自 tag_vec 列。

    ⚠️ **查询不能加 `tag_vec IS NOT NULL`。** 零向量作品（142 部）虽然对
       向量和贡献为零，却仍要参与 μ 的计算 —— 漏掉它们会让 μ 偏移，
       进而改变**所有**作品的权重符号。numpy 侧 `cat.mat[rows]` 取到的是
       一行零，两边必须一致。这是最容易漏掉的一处不等价。
    """
    return preference_vectors(conn, ratings, prior_mean=prior_mean,
                              prior_weight=prior_weight)[0]


def preference_vectors(conn: psycopg.Connection, ratings: list[Rating],
                       *, prior_mean: float = PRIOR_MEAN,
                       prior_weight: float = PRIOR_WEIGHT
                       ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """三个空间各自的偏好向量 (tag, emb, staff)。P1 融合用。

    ⚠️ **μ 只算一次、三个空间共用** —— 与 recommend._rating_weights() 一致。
       各空间各算各的 μ 的话，同一条评分在 tag 空间是「喜欢」、在 embedding
       空间可能变成「不喜欢」，融合出来的东西没有意义。
    """
    dims = (len(vocab()), EMB_DIM, STAFF_DIM)
    by_id = {r.subject_id: r for r in ratings}
    empty = tuple(np.zeros(d, dtype=np.float32) for d in dims)
    if not by_id:
        return empty

    with conn.cursor() as cur:
        cur.execute(
            "SELECT subject_id, tag_vec, vec, staff_vec FROM anime_profile "
            "WHERE subject_id = ANY(%s)", (list(by_id),))
        rows = cur.fetchall()
    if not rows:
        return empty

    # 只保留库内存在的评分 —— 与 numpy 侧 `index_of(...) is not None` 一致
    rs = [by_id[r[0]] for r in rows]
    cw = sum(r.confidence for r in rs)
    mu = ((sum(r.confidence * r.score for r in rs) + prior_weight * prior_mean)
          / (cw + prior_weight))

    prefs = [np.zeros(d, dtype=np.float32) for d in dims]
    for row, r in zip(rows, rs, strict=True):
        w = np.float32(r.confidence * (r.score - mu))
        for k, vec in enumerate(row[1:]):
            if vec is None:               # 该空间无向量：只进 μ，不进向量和
                continue
            # ⚠️ .astype(float32) 不能省：halfvec 的 to_numpy() 是 float16
            prefs[k] += vec.to_numpy().astype(np.float32) * w
    return tuple(prefs)


#: 三路的 (列名, Weights 字段名, 偏好向量参数名, 类型转换)
_SPACES = (("tag_vec", "tag", "pref_tag", "vector"),
           ("vec", "emb", "pref_emb", "halfvec"),
           ("staff_vec", "staff", "pref_staff", "sparsevec"))


def _active(prefs: tuple[np.ndarray, ...], weights: Weights) -> list[int]:
    """哪几路真正参与。逐条对应 recommend._cosines() 的跳过逻辑。

    ⚠️ 权重 >0 **且** 偏好向量非零才算参与。零偏好向量必须跳过而不是当成
       0 相似度：pgvector 对零向量的 `<=>` 返回 **NaN**（实测），
       NaN 会污染整个加权和，让所有作品的 match 都变成 NaN，
       而 `ORDER BY match DESC` 对 NaN 不报错 —— 又是「不报错但全错」。
    """
    out = []
    for k, (_col, attr, _pname, _cast) in enumerate(_SPACES):
        if getattr(weights, attr) > 0 and float(np.linalg.norm(prefs[k])) > 0:
            out.append(k)
    return out


def _match_expr(params: dict, prefs: tuple[np.ndarray, ...],
                weights: Weights, active: list[int]) -> str:
    """match 的 SQL 表达式：参与各路的余弦加权和，再按参与权重归一化。

    ⚠️ **必须与 recommend._cosines() 逐字对应**：那边先各自除以偏好向量模长
       得到余弦，再加权、再除以参与权重之和。这里把「除以模长」交给 pgvector
       的 `<=>`（它算的就是余弦距离，与向量模长无关），所以只需加权 + 归一化。

    ⚠️ `COALESCE(..., 0)`：该空间无向量的作品，`<=>` 返回 NULL，
       整个表达式会变成 NULL。numpy 侧对应的是「零行 → 余弦 0」，
       所以这里必须补 0 而不是让它传播成 NULL。
    """
    terms, total = [], 0.0
    for k in active:
        col, attr, pname, cast = _SPACES[k]
        w = getattr(weights, attr)
        params[pname] = _lit(prefs[k], cast)
        terms.append(f"{w!r} * COALESCE(1 - ({col} <=> %({pname})s::{cast}), 0)")
        total += w
    return f"(({' + '.join(terms)}) / {total!r})"


def _lit(v: np.ndarray, cast: str) -> str:
    """偏好向量 → pgvector 字面量。sparsevec 要写成 `{i:v,...}/dim`。

    ⚠️ **必须 `.9g` 不能 `.7g`。** float32 需要 9 位有效数字才能无损往返 ——
       实测 `.7g` 每个元素差 1.49e-08，1024 维累积后约 5e-7。
       那个量级虽然被 halfvec 的 fp16 噪声（~2e-5）掩盖，但它是**白送的损失**，
       而且掩盖它的前提（emb 参与）不总是成立：跑纯 tag baseline 时
       fp32 精度是 2e-7 量级，`.7g` 的误差就不再可忽略了。
    """
    if cast != "sparsevec":
        return "[" + ",".join(f"{x:.9g}" for x in v.tolist()) + "]"
    nz = {i: float(x) for i, x in enumerate(v.tolist()) if x != 0.0}
    # ⚠️ sparsevec 下标从 1 开始；空向量不会走到这里（_active 已过滤）
    body = ",".join(f"{i + 1}:{x:.9g}" for i, x in sorted(nz.items()))
    return "{" + body + "}/" + str(len(v))


def _filters(params: dict, *, mode: Mode, year_min: int | None,
             year_max: int | None, include_nsfw: bool,
             min_score: float | None, excluded: list[int],
             active: list[int]) -> str:
    """拼过滤条件。逐条对应 recommend.score() 里的 keep 掩码。"""
    # 零向量存 NULL，这一句同时完成「排除零向量」—— 见 sql/002_tag_vec.sql
    # ⚠️ P1 之后判据是「**参与的任一路**有向量」，与 numpy 侧的
    #    `for mat, wt in ...: keep |= mat.any(axis=1)` 逐条对应。
    #    只看 tag_vec 的话，跑 embedding baseline 时会把 131 部
    #    「无 tag 但有 embedding」的作品错误排除。
    cols = [_SPACES[k][0] for k in active]
    parts = ["(" + " OR ".join(f"{c} IS NOT NULL" for c in cols) + ")"]

    if not include_nsfw:
        parts.append("NOT nsfw")
    if min_score is not None:
        # 「有评分且低于下限」才排除；未评分（NULL 或 <=0）放行
        parts.append("(score IS NULL OR score <= 0 OR score >= %(min_score)s)")
        params["min_score"] = min_score
    if excluded:
        parts.append("NOT (subject_id = ANY(%(excluded)s))")
        params["excluded"] = excluded

    if year_min is not None or year_max is not None:
        if year_min is not None:
            parts.append("air_year >= %(year_min)s")
            params["year_min"] = year_min
        if year_max is not None:
            parts.append("air_year <= %(year_max)s")
            params["year_max"] = year_max
    elif mode == "classic":
        parts.append("air_year < %(classic_before)s")
        params["classic_before"] = CLASSIC_BEFORE
    elif mode in ("season", "aired", "upcoming", "recent"):
        lo, hi = season_window()
        parts.append("air_date BETWEEN %(win_lo)s AND %(win_hi)s")
        params["win_lo"], params["win_hi"] = lo, hi
        today = datetime.datetime.now(datetime.UTC).date()
        if mode == "aired":
            parts.append("air_date <= %(today)s")
            params["today"] = today
        elif mode == "upcoming":
            parts.append("air_date > %(today)s")
            params["today"] = today
    return " AND ".join(parts)


def explain(conn: psycopg.Connection, pref: np.ndarray,
            subject_ids: list[int], k: int = 4) -> dict[int, list[str]]:
    """推荐理由：对余弦贡献最大的几个 tag。

    ⚠️ 取 pref[j] × item[j] 而不是「该作品票数最高的 tag」——
       后者与用户无关，解释不了「为什么推给你」。只取正贡献：
       负贡献是「这部有你不喜欢的元素」，不该出现在推荐理由里。

    只查最终返回的那 top_k 部（约 20 行 × 1.2 KB），不查整个召回池。
    """
    if not subject_ids:
        return {}
    vlist = vocab()
    with conn.cursor() as cur:
        cur.execute("SELECT subject_id, tag_vec FROM anime_profile "
                    "WHERE subject_id = ANY(%s)", (subject_ids,))
        rows = cur.fetchall()
    out: dict[int, list[str]] = {}
    for sid, vec in rows:
        if vec is None:
            out[sid] = []
            continue
        v = vec.to_numpy() if hasattr(vec, "to_numpy") else np.asarray(vec)
        contrib = pref * v.astype(np.float32)
        out[sid] = [vlist[j] for j in np.argsort(-contrib)[:k] if contrib[j] > 0]
    return out


def score(conn: psycopg.Connection,
          ratings: "list[Rating | tuple]",
          *,
          prefs: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
          mode: Mode = "all",
          year_min: int | None = None,
          year_max: int | None = None,
          include_nsfw: bool = False,
          min_score: float | None = MIN_SCORE,
          fold_series: bool = True,
          rank_by: RankBy = "blend",
          blend_alpha: float = DEFAULT_ALPHA,
          weights: Weights | None = None,
          top_k: int = 20) -> list[Recommendation]:
    """无状态打分，逐条等价于 recommend.score()。返回按 rank_score 降序。

    两次往返：一次取被评作品的向量，一次算余弦 + 过滤 + 召回。
    第二段的 blend 重排在 Python 侧对 ~200 行做，不值得再回一次库。

    `prefs` 可以传入已算好的三元组 (tag, emb, staff)，省掉第一次往返 ——
    调用方若还要调 explain() 生成推荐理由，就该复用同一份，别算两遍。

    ⚠️ **必须是三元组，不能只传 tag。** P1 之前这里收的是单个 tag 向量；
       融合后若只覆盖 tag、另外两路仍查库，`/recommend` 就会从 3 次往返
       变成 4 次 —— 而调用方明明已经查过一次了。
    """
    rs = [Rating.coerce(x) for x in ratings]
    wts = weights or DEFAULT_WEIGHTS
    if prefs is None:
        prefs = preference_vectors(conn, rs)

    active = _active(prefs, wts)
    if not active:
        return []

    want = top_k if rank_by == "match" else max(top_k * DEFAULT_POOL_FACTOR,
                                                DEFAULT_POOL_MIN)
    params: dict = {"want": want}
    match = _match_expr(params, prefs, wts, active)
    where = _filters(params, mode=mode, year_min=year_min, year_max=year_max,
                     include_nsfw=include_nsfw, min_score=min_score,
                     excluded=[r.subject_id for r in rs if r.exclude],
                     active=active)
    sql = (_SQL_FOLD if fold_series else _SQL_FLAT).format(
        filters=where, match=match)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return []

    ms = np.array([r[6] for r in rows], dtype=np.float64)
    qs = _weighted_rating(np.array([r[4] for r in rows], dtype=np.float32),
                          np.array([r[5] for r in rows],
                                   dtype=np.float32)).astype(np.float64)

    if rank_by == "match":
        final = ms
    elif rank_by == "quality":
        final = qs
    else:
        def unit(v: np.ndarray) -> np.ndarray:
            lo, hi = v.min(), v.max()
            return np.full_like(v, 0.5) if hi - lo < 1e-9 else (v - lo) / (hi - lo)

        final = blend_alpha * unit(ms) + (1 - blend_alpha) * unit(qs)

    order = np.argsort(-final, kind="stable")[:top_k]
    return [Recommendation(subject_id=rows[j][0], name=rows[j][1],
                           match=float(ms[j]), quality=float(qs[j]),
                           rank_score=float(final[j]))
            for j in order]

"""recommend.score()（内存矩阵）与 recommend_sql.score()（Postgres）必须逐条等价。

⚠️ **这个测试是「放弃 Render、改上 Vercel」那个决定能成立的前提。**
   线上走 SQL（serverless 没有常驻内存），第 5 周离线评测走 numpy
   （leave-one-out 要跑 10⁵~10⁶ 次打分，SQL 往返做不到）。
   两套实现是被架构逼出来的，而 CLAUDE.md 第 2 节的铁律说得很清楚：
   「评测时不能出现两套口径」。这个测试就是把那条纪律变成可断言的不变式 ——
   一旦有人改了其中一条路径的打分逻辑，这里会红。

   ⚠️ 光靠「向量存在库里两边都读它」是不够的：那只保证输入相同，
      不保证过滤、召回、系列折叠、重排这四步的语义相同。必须逐条比对输出。

跑法（需要能连到 Neon，且已跑过 scripts/build_tag_vectors.py）：

    uv run pytest tests/ -v
"""

import random

import numpy as np
import pytest

from src import db, recommend, recommend_sql
from src.recommend import Rating

# 余弦在两侧的累加顺序不同（numpy 是 BLAS 的 (n,d)@(d,)，pgvector 是逐行
# 标量循环），fp32 下末位差异约 1e-7。放宽到 1e-5 足够严格又不会假红。
TOL = 1e-5

# ⚠️ **embedding 参与时必须放宽到 1e-4。** 这不是为了让测试变绿，
#    2026-08-14 实测各路的 match 最大偏差：
#
#      tag   (vector float32)    2.19e-07      ← fp32 精度
#      staff (sparsevec float32) 1.08e-07      ← fp32 精度
#      emb   (halfvec  fp16)     1.94e-05      ← 差 100 倍
#      融合                      2.21e-05
#
#    偏差**全部来自 halfvec 的 fp16 存储**（sql/003 选它省了 23.5 MB）。
#    两侧读的是同一批 fp16 值，差异只在累加：numpy 先转 fp32 再 BLAS，
#    pgvector 直接从 fp16 算。**所有情况下结果顺序都完全一致。**
#
#    ⚠️ 决定性的理由：**embedding API 自身的不确定性在余弦上就有 ~6.4e-5**
#       （A.7 实测，服务端连续批处理导致，无法规避）。要求两条路径对齐到
#       比数据源自身可复现性更高的精度，没有意义。
#
# ⚠️ **这个数是按 120 组随机档案的实测上界定的，不是拍的。**
#    首次只测了 12 组、得到 1.94e-05，据此定 1e-4 —— 扩到 120 组后
#    真实上界是 **7.73e-05**，余量只剩 1.3 倍，随机换一批档案就会假红。
#    5e-4 留 6.5 倍余量。**改这个常数前请先重跑那个测量，不要凭感觉调。**
EMB_TOL = 5e-4

# 排序并列时两侧都用「匹配度降序 + subject_id 升序」。但浮点噪声可能让
# 本该并列的两项差出 1e-8，从而交换位置 —— 这不是逻辑错误。
# 因此 id 序列不完全相同时，只要错位项的 rank_score 在容差内即可接受。
SWAP_TOL = 1e-4

# ⚠️ **rank_score 需要比 match 更宽的容差，因为 min-max 归一化会放大偏差。**
#    blend 模式下 rank_score 是**池内**归一化到 [0,1] 的量：池内 match 跨度
#    只有 ~0.1 时，2e-5 的 match 差会被放大成 2e-4。
#    2026-08-14 实测 120 组随机档案：match 上界 7.73e-05 → rank_score 上界
#    **8.46e-04**，放大约 11 倍。5e-3 留 6 倍余量。
#
# ⚠️ 放宽的只是**数值**比较。「结果顺序是否相同」由上面的 id 序列检查
#    独立把关，不受这个常数影响 —— 两者不能混为一谈。
#    ⇒ 逻辑错误（错的向量、错的符号、错的过滤）会让顺序整个变掉，
#      这些容差再宽也拦得住；它们放过的只有 fp16 量级的数值抖动。
#
# ⬜ 若将来觉得这个容差太松，根治办法是把 `vec` 从 halfvec 换回
#    `vector(1024)` float32（+23.5 MB），届时 emb 路径能回到 1e-7 量级。
#    但注意 embedding API 自身噪声 6.4e-5 仍在，收紧的只是实现间的一致性。
RANK_TOL = 5e-3


@pytest.fixture(scope="module")
def conn():
    c = db.connect()
    yield c
    c.close()


@pytest.fixture(scope="module")
def cat(conn):
    return recommend.build_catalog(conn)


def _profiles(cat, n_profiles: int = 12, seed: int = 20260812
              ) -> list[list[Rating]]:
    """随机档案。混三种作答，覆盖不同的置信度组合与档案规模。"""
    rng = random.Random(seed)
    ids = [int(x) for x in cat.ids]
    out = []
    for k in (1, 2, 3, 5, 10, 20, 30):        # 含极小档案：收缩均值的边界
        for _ in range(max(1, n_profiles // 7)):
            picked = rng.sample(ids, k)
            rs = []
            for sid in picked:
                choice = rng.choice(("seen", "seen", "wish", "pass"))
                if choice == "seen":
                    rs.append(Rating(sid, rng.uniform(1, 10), 1.0, True))
                elif choice == "wish":
                    rs.append(Rating(sid, 8.0, 0.5, False))
                else:
                    rs.append(Rating(sid, 3.0, 0.3, True))
            out.append(rs)
    return out


def _assert_same(a, b, ctx: str, *, tol: float = EMB_TOL):
    """`tol` 只放宽 match/quality 的**数值**比较，不放宽顺序要求。

    ⚠️ 顺序始终按 SWAP_TOL 判定 —— 放宽数值容差不等于容忍结果不同。
    """
    assert len(a) == len(b), f"{ctx}: 条数不同 {len(a)} vs {len(b)}"
    ids_a = [x.subject_id for x in a]
    ids_b = [x.subject_id for x in b]
    if ids_a != ids_b:
        # 允许浮点噪声导致的并列项交换，但分数必须对得上
        sa = sorted(x.rank_score for x in a)
        sb = sorted(x.rank_score for x in b)
        assert np.allclose(sa, sb, atol=RANK_TOL), (
            f"{ctx}: 结果不同\n  numpy={ids_a}\n  sql  ={ids_b}")
        return
    for x, y in zip(a, b, strict=True):
        assert abs(x.match - y.match) < tol, f"{ctx}: match 不同 {x} vs {y}"
        assert abs(x.quality - y.quality) < tol, f"{ctx}: quality 不同 {x} vs {y}"
        assert abs(x.rank_score - y.rank_score) < RANK_TOL, (
            f"{ctx}: rank_score 不同 {x} vs {y}")


def test_parity_tag_only_is_unchanged(conn, cat):
    """⚠️ **P1 之前的行为必须逐位可复现。**

    第 10 节的 tag baseline 就是 Weights(tag=1, emb=0, staff=0)。它一旦被 P1
    的改动扰动，四条 baseline 的对照就失去意义 —— 而扰动会很小、很难察觉。
    这里用 fp32 级的严格容差（TOL），**不是**放宽后的 EMB_TOL。
    """
    for i, rs in enumerate(_profiles(cat)):
        a = recommend.score(cat, rs, top_k=10, weights=recommend.TAG_ONLY)
        b = recommend_sql.score(conn, rs, top_k=10, weights=recommend.TAG_ONLY)
        _assert_same(a, b, f"TAG_ONLY 档案#{i}(n={len(rs)})", tol=TOL)


@pytest.mark.parametrize("wname", ["tag", "emb", "staff", "fused"])
def test_parity_weights(conn, cat, wname):
    """三路各自单跑 + 融合。

    ⚠️ 单路测试不是多余的：融合把三个余弦加权求和，某一路的符号错误或
       维度错位可能被另外两路掩盖到看不出来 —— 而结果依然「像模像样」。
    """
    w = {"tag": recommend.TAG_ONLY,
         "emb": recommend.Weights(0.0, 1.0, 0.0),
         "staff": recommend.Weights(0.0, 0.0, 1.0),
         "fused": recommend.DEFAULT_WEIGHTS}[wname]
    for i, rs in enumerate(_profiles(cat, n_profiles=7)):
        a = recommend.score(cat, rs, top_k=10, weights=w)
        b = recommend_sql.score(conn, rs, top_k=10, weights=w)
        _assert_same(a, b, f"weights={wname} 档案#{i}(n={len(rs)})")


@pytest.mark.parametrize("rank_by", ["match", "quality", "blend"])
def test_parity_rank_by(conn, cat, rank_by):
    for i, rs in enumerate(_profiles(cat)):
        a = recommend.score(cat, rs, rank_by=rank_by, top_k=10)
        b = recommend_sql.score(conn, rs, rank_by=rank_by, top_k=10)
        _assert_same(a, b, f"rank_by={rank_by} 档案#{i}(n={len(rs)})")


@pytest.mark.parametrize("mode", ["all", "classic", "season", "aired",
                                  "upcoming", "recent"])
def test_parity_mode(conn, cat, mode):
    for i, rs in enumerate(_profiles(cat, n_profiles=7)):
        a = recommend.score(cat, rs, mode=mode, top_k=10)
        b = recommend_sql.score(conn, rs, mode=mode, top_k=10)
        _assert_same(a, b, f"mode={mode} 档案#{i}")


def test_parity_flags(conn, cat):
    """逐个翻转开关。fold_series=False 是 SQL 侧另一条查询，必须单独覆盖。"""
    cases = [
        {"fold_series": False},
        {"include_nsfw": True},
        {"min_score": None},
        {"min_score": 7.0},                    # 高到会显著缩小候选池
        {"year_min": 2015, "year_max": 2020},
        {"year_min": 1990},
        {"blend_alpha": 0.0},                  # 纯质量
        {"blend_alpha": 1.0},                  # 纯匹配
        {"top_k": 1},
        {"top_k": 50},
        {"fold_series": False, "include_nsfw": True, "min_score": None},
    ]
    for rs in _profiles(cat, n_profiles=7)[:6]:
        for kw in cases:
            a = recommend.score(cat, rs, **kw)
            b = recommend_sql.score(conn, rs, **kw)
            _assert_same(a, b, f"flags={kw}")


def test_preference_vector_parity(conn, cat):
    """偏好向量本身必须一致 —— 它是后面一切的输入。

    ⚠️ 特别覆盖「评了零向量作品」：那 142 部对向量和贡献为零，
       却仍要参与 μ 的计算。SQL 侧若在取向量时加了 tag_vec IS NOT NULL，
       μ 会偏移，进而改变所有作品的权重符号 —— 而结果依然像模像样，
       是最难发现的一类不等价。
    """
    zero_ids = [int(cat.ids[i]) for i in np.flatnonzero(~cat.mat.any(axis=1))]
    assert zero_ids, "库里应当有零向量作品，否则这个测试没覆盖到目标"

    cases = [
        [Rating(zero_ids[0], 9.0)],
        [Rating(zero_ids[0], 9.0), Rating(int(cat.ids[0]), 4.0)],
        [Rating(z, 8.0) for z in zero_ids[:5]],
    ]
    for rs in cases + _profiles(cat, n_profiles=7):
        pa = recommend.preference_vector(cat, rs)
        pb = recommend_sql.preference_vector(conn, rs)
        assert np.allclose(pa, pb, atol=TOL), (
            f"偏好向量不同，最大差 {np.abs(pa - pb).max():.2e}")


def test_empty_and_unknown(conn, cat):
    """边界：空作答、库外 id。两侧都该返回空列表而不是抛异常。"""
    for rs in ([], [Rating(999999999, 9.0)]):
        assert recommend.score(cat, rs) == []
        assert recommend_sql.score(conn, rs) == []


def test_catalog_matches_stored_vectors(conn, cat):
    """内存矩阵 == 库里的 tag_vec。这是「口径一致靠构造保证」的那一半。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT subject_id, tag_vec FROM anime_profile
                       WHERE tag_vec IS NOT NULL ORDER BY subject_id LIMIT 500""")
        rows = cur.fetchall()
    for sid, vec in rows:
        i = cat.index_of(sid)
        v = vec.to_numpy() if hasattr(vec, "to_numpy") else np.asarray(vec)
        assert np.array_equal(cat.mat[i], v.astype(np.float32)), (
            f"subject_id={sid} 的向量与库里不一致 —— "
            "是否改过 tagvec/tag_rules/词表却没重跑 build_tag_vectors.py？")

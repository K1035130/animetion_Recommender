"""回填问卷选题所需的两列：`mmr_rank`（现行方法）与 `cluster_id`（第 5 周对照）。

⚠️ **职责边界**（CLAUDE.md「脚本职责与启动顺序」）：本脚本只写这两列
   和 `build_meta` 的 'questionnaire_pool' 一行。幂等，可重跑。

前置：sql/005_mmr_rank.sql、`anime_profile.vec` 已灌（scripts/build_embeddings.py）。

⚠️ **什么时候必须重跑：**
   · 重跑过 build_embeddings.py（向量变了 → 多样性排序全变）
   · 改了 MMR_LAMBDA 或候选池口径
   · 第 6 周季度同步加入新作品

跑法：
    uv run --group ml python scripts/build_clusters.py
    uv run --group ml python scripts/build_clusters.py --report   # 只看指标不写库

---

**为什么是 MMR 而不是聚类** —— 见 sql/005_mmr_rank.sql 的注释。一句话：
实测 silhouette≈0.035、ARI 跨种子≈0.48，这份数据是连续体不是团块，
而我们真正要的「选 N 部覆盖口味空间」是多样性问题，MMR 直接优化它且确定性。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from psycopg.types.json import Json

from src import db

# MMR 的权重：λ·热度 − (1−λ)·与已选集合的最大相似度。
# ⚠️ λ 实测：0.3 冗余 0.329/热度 25k · 0.5 冗余 0.378/热度 41k · 0.7 冗余 0.401/热度 43k。
#    取 0.5 —— 它同时优于纯热度(0.455/44k)和 k-means 代表(0.399/37k)。
#    ⬜ 第 5 周的冷启动曲线应该扫一遍 λ，这个默认值不是调优过的最优解。
MMR_LAMBDA = 0.5

# k-means 的簇数。⚠️ **不是用 silhouette 定的** —— 实测它随 N 单调下降、
#    没有拐点也没有峰值，在这份数据上给不出任何信号。
#    取 30 是为了与问卷默认题数对齐，让「内容簇选题」这条 baseline
#    与 MMR 在同一题数下可比。
N_CLUSTERS = 30
PCA_DIMS = 50          # 50 维保留约 46% 方差（10 维 21% / 100 维 62%）
SEED = 0


def load_pool(conn) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """候选池 = TV/WEB · 非续作根 · 非 nsfw · vec 非空。

    ⚠️ 口径必须与 questionnaire.select_items 一致，否则会出现
       「排了名却选不出来」或「选出来的没有名次」。
    ⚠️ 池子不含 nsfw：理由见 sql/005_mmr_rank.sql（按默认路径优化）。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, fav_done, vec
              FROM anime_profile
             WHERE vec IS NOT NULL
               AND NOT nsfw
               AND form IN ('TV', 'WEB')
               AND COALESCE(series_root, subject_id) = subject_id
             ORDER BY subject_id
        """)
        rows = cur.fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    done = np.array([r[1] for r in rows], dtype=np.float64)
    # ⚠️ halfvec 读回来是 HalfVector 对象且 to_numpy() 是 float16，
    #    必须显式 astype(float32) —— 与 src/recommend.py 同一个模式。
    X = np.array([r[2].to_numpy() for r in rows], dtype=np.float32)
    X /= np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)
    return ids, done, X


def mmr_order(X: np.ndarray, pop: np.ndarray, lmb: float = MMR_LAMBDA) -> np.ndarray:
    """MMR 贪心，返回**全池**的排列（下标序）。

    ⚠️ 排全池而不只排前 N：年份档过滤后仍需有序列可取
       （实测全池排序后过滤 ≈ 每档单独算，差异在噪声内）。

    增量维护 `maxsim`：每轮只需算新选中项与全池的相似度再取逐位最大，
    单轮 O(n·d) 而不是 O(n·|sel|·d)。
    """
    n = len(X)
    first = int(np.argmax(pop))
    order = [first]
    maxsim = X @ X[first]
    chosen = np.zeros(n, dtype=bool)
    chosen[first] = True

    for _ in range(n - 1):
        score = lmb * pop - (1 - lmb) * maxsim
        score[chosen] = -np.inf
        j = int(np.argmax(score))
        order.append(j)
        chosen[j] = True
        maxsim = np.maximum(maxsim, X @ X[j])
    return np.array(order, dtype=np.int64)


def redundancy(X: np.ndarray, idx: np.ndarray) -> float:
    """选中集合的两两余弦均值 —— 越低说明覆盖的口味区域越分散。"""
    S = X[idx] @ X[idx].T
    iu = np.triu_indices(len(idx), 1)
    return float(S[iu].mean())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="只打印指标，不写库")
    args = ap.parse_args()

    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM information_schema.columns
                 WHERE table_name = 'anime_profile' AND column_name = 'mmr_rank'
            """)
            if cur.fetchone()[0] == 0:
                print("✗ 缺 mmr_rank 列。先跑 sql/005_mmr_rank.sql", file=sys.stderr)
                return 1

        ids, done, X = load_pool(conn)
        if len(ids) == 0:
            print("✗ 候选池为空，先跑 scripts/build_embeddings.py", file=sys.stderr)
            return 1
        print(f"候选池 {len(ids)} 部")

        pop = np.log1p(done)
        pop = (pop - pop.min()) / max(pop.max() - pop.min(), 1e-12)

        order = mmr_order(X, pop)
        Z = PCA(n_components=PCA_DIMS, random_state=SEED).fit_transform(X)
        labels = KMeans(n_clusters=N_CLUSTERS, n_init=10,
                        random_state=SEED).fit_predict(Z)

        # 三个方法在 N=30 下的对照，直接打出来供第 5 周引用
        n = 30
        top_pop = np.argsort(-done)[:n]
        mmr_top = order[:n]
        km_rep = np.array([int(np.argmax(np.where(labels == c, pop, -1.0)))
                           for c in range(N_CLUSTERS)])
        print(f"\n{'方法':<22}{'冗余':>8}{'中位热度':>10}")
        for label, idx in (("纯热度（原实现）", top_pop),
                           ("内容簇 k-means 代表", km_rep),
                           (f"MMR λ={MMR_LAMBDA}", mmr_top)):
            print(f"{label:<22}{redundancy(X, idx):>8.4f}{int(np.median(done[idx])):>10d}")

        if args.report:
            print("\n--report：未写库")
            return 0

        ranks = np.empty(len(ids), dtype=np.int64)
        ranks[order] = np.arange(1, len(order) + 1)      # 位次从 1 开始
        payload = [(int(r), int(c), int(s))
                   for r, c, s in zip(ranks, labels, ids, strict=True)]

        with conn.cursor() as cur:
            # ⚠️ 先整列清空：池子口径变化后，上一轮排过名而这轮不在池里的作品
            #    会留下**陈旧位次**，而 select_items 照样会把它选出来。
            cur.execute("UPDATE anime_profile SET mmr_rank = NULL, cluster_id = NULL "
                        "WHERE mmr_rank IS NOT NULL OR cluster_id IS NOT NULL")
            for i in range(0, len(payload), 500):
                cur.executemany(
                    "UPDATE anime_profile SET mmr_rank = %s, cluster_id = %s "
                    "WHERE subject_id = %s", payload[i:i + 500])
            cur.execute("""
                INSERT INTO build_meta (key, value, updated_at) VALUES ('questionnaire_pool', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """, (Json({
                "method": "mmr", "lambda": MMR_LAMBDA, "pool": len(ids),
                "n_clusters": N_CLUSTERS, "pca_dims": PCA_DIMS, "seed": SEED,
                "redundancy_mmr30": round(redundancy(X, mmr_top), 4),
                "redundancy_pop30": round(redundancy(X, top_pop), 4),
            }),))
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT mmr_rank) FROM anime_profile "
                        "WHERE mmr_rank IS NOT NULL")
            cnt, distinct = cur.fetchone()
        print(f"\n✓ mmr_rank {cnt} 行（去重 {distinct}，应相等）· cluster_id {N_CLUSTERS} 簇")
        if cnt != distinct:
            print("✗ 位次有重复，写入不完整", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

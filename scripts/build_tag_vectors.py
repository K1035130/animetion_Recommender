"""回填 anime_profile.tag_vec 与 series_root。

⚠️ **职责边界**（CLAUDE.md「脚本与职责边界」）：本脚本只写这两列，
   绝不碰 dump 派生列 / staff / AniList 列。幂等，可任意顺序重跑。

⚠️ **什么时候必须重跑：**
   · 改了 data/interim/tag_vocab.json（词表变了 → 维度变了）
   · 改了 src/tag_rules.py 的 normalize()/classify()
   · 改了 src/tagvec.py 的加权逻辑
   · 第 6 周季度同步加入新作品（idf 依赖全库词频，**只能全量重算**）
   · 重跑过 scripts/build_series_map.py

前置：data/interim/series_root.json（由 scripts/build_series_map.py 产出）。
缺它时 series_root 全部填自身，脚本会明确报警而不是静默降级 ——
后者的表现是「推荐列表里突然全是第二季第三季」，日志里一个字都没有。

    uv run python scripts/build_tag_vectors.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from src import db, series, tagvec


def main() -> None:
    conn = db.connect()


    with conn.cursor() as cur:
        # ⚠️ 顺序必须固定且与写回一致 —— idf 按全库统计，行顺序错了向量就错位
        cur.execute("""
            SELECT subject_id, tags, meta_tags
            FROM anime_profile ORDER BY subject_id
        """)
        rows = cur.fetchall()
    print(f"读入 {len(rows)} 部作品")

    vlist, mat = tagvec.compute(rows)
    nonzero = (mat != 0).any(axis=1)
    print(f"向量 {mat.shape}，非零 {nonzero.sum()} 部，"
          f"零向量 {(~nonzero).sum()} 部（存 NULL）")
    print(f"平均非零维 {(mat != 0).sum(axis=1).mean():.1f} / {len(vlist)}")

    smap = series.load(required=False)
    if not smap:
        print("⚠️  警告：没有找到 series_root.json，series_root 将全部填自身。"
              "续作折叠会失效 —— 先跑 scripts/build_series_map.py")
    else:
        print(f"系列映射 {len(smap)} 条续作 → "
              f"{len(set(smap.values()))} 个系列根")

    # 零向量写 NULL：它们与任何偏好向量的余弦都是 0，而偏好向量整体为负时
    # 0 反而高于所有负相关作品，必须排除在候选之外。存 NULL 才能用
    # `tag_vec IS NOT NULL` 一句话过滤。
    payload = [
        (mat[i] if nonzero[i] else None,
         smap.get(int(sid), int(sid)),
         int(sid))
        for i, (sid, _, _) in enumerate(rows)
    ]

    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE anime_profile SET tag_vec = %s, series_root = %s "
            "WHERE subject_id = %s",
            payload,
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE tag_vec IS NOT NULL),
                   count(*) FILTER (WHERE series_root IS NULL),
                   count(DISTINCT series_root)
            FROM anime_profile
        """)
        has_vec, no_root, n_series = cur.fetchone()
    print(f"\n写回完成：tag_vec 非空 {has_vec} 行，"
          f"series_root 为空 {no_root} 行，共 {n_series} 个系列")

    # 抽验一条，确认库里的向量和内存里的逐位相同
    with conn.cursor() as cur:
        cur.execute("SELECT tag_vec FROM anime_profile WHERE subject_id = %s",
                    (int(rows[0][0]),))
        got = cur.fetchone()[0]
    if got is not None:
        # pgvector 0.5 的 register_vector 回读的是 Vector 对象，不是 ndarray
        got = got.to_numpy() if hasattr(got, "to_numpy") else np.asarray(got)
        delta = float(np.abs(got.astype(np.float32) - mat[0]).max())
        print(f"抽验 subject_id={rows[0][0]}：与内存向量最大逐位差 {delta:.2e}")

    # ⚠️ 批量 UPDATE 会留下旧行版本（MVCC），库占用会虚涨。
    #    第 5 节：三轮回填后从 43 MB 涨到 99 MB，VACUUM FULL 后回到 44 MB。
    print("\n⚠️  批量 UPDATE 产生了 MVCC 膨胀，记得跑：VACUUM FULL anime_profile;")
    conn.close()


if __name__ == "__main__":
    main()

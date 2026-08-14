"""回填 anime_profile.vec —— summary 的 Qwen3-Embedding 向量。

⚠️ **职责边界**（CLAUDE.md「脚本职责与启动顺序」）：本脚本只写 `vec` 一列
   和 `build_meta` 的 'embed_vec' 一行，绝不碰 tag_vec / staff / AniList 列。
   幂等，可任意顺序重跑。

前置：
  · sql/003_vec_halfvec.sql（vec 必须已是 halfvec(1024)）
  · sql/004_build_meta.sql
  · .env 里的 SILICONFLOW_API_KEY

⚠️ **什么时候必须重跑：**
   · 改了 src/embed.py 的 MODEL / DIM / SOURCE_FIELD（指纹会变）
   · 第 6 周季度同步加入新作品（新作品的 vec 是 NULL，打分召不回它们）
   ⚠️ 改 QUERY_INSTRUCT **不需要**重跑 —— 库里存的是文档向量（不加前缀）。

跑法：
    uv run --group etl python scripts/build_embeddings.py --limit 50   # 先小样本
    uv run --group etl python scripts/build_embeddings.py              # 再全量
    psql -c 'VACUUM FULL anime_profile'                                # ⚠️ 别漏

⚠️ **先跑 --limit 50 看一遍**（第 15 节原则 5）。全量约 2 分钟 / ¥0.14，
   不贵，但字段理解错了的话小样本几秒就能发现。

成本与耗时（A.7 实测）：11,453 部里非空 summary 约 10,864 条 ≈ 200 万 token
≈ ¥0.14 ≈ 2 分钟（TPM=1,000,000 是唯一瓶颈，RPM=2,000 不是）。
⚠️ 重跑时命中缓存的部分不花钱也不花时间。
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from pgvector import HalfVector
from psycopg.types.json import Json
from tqdm import tqdm

from src import db, embed, embed_cache

# TPM 上限 1,000,000，取 80% 留余量。
# ⚠️ 按 **token** 节流不是按请求 —— RPM 2,000 在 batch=32 时根本够不着。
TOKENS_PER_MIN = 800_000

# 保守估计：中文实测约 0.7 token/字，这里按 1.0 算。
# 高估只会多等一会儿，低估会撞限流 —— 不对称，所以往高了估。
TOKENS_PER_CHAR = 1.0


class TokenBudget:
    """按 token 的滑动窗口节流。"""

    def __init__(self, per_min: int = TOKENS_PER_MIN) -> None:
        self.per_min = per_min
        self.window: deque[tuple[float, int]] = deque()

    def take(self, tokens: int) -> None:
        now = time.monotonic()
        while self.window and now - self.window[0][0] > 60.0:
            self.window.popleft()
        used = sum(t for _, t in self.window)
        if used + tokens > self.per_min and self.window:
            sleep = 60.0 - (now - self.window[0][0])
            if sleep > 0:
                time.sleep(sleep)
            return self.take(tokens)
        self.window.append((now, tokens))


def fetch_rows(conn, limit: int | None) -> list[tuple[int, str]]:
    """取 (subject_id, summary)，只要非空的。

    ⚠️ summary 为空的 589 部**不在结果里，它们的 vec 保持 NULL** —— 与 tag_vec
       同一条理由（sql/002）：零向量与任何偏好向量的余弦都是 0，而偏好向量
       整体为负时 0 反而高于所有负相关作品。
    """
    sql = """
        SELECT subject_id, summary
          FROM anime_profile
         WHERE coalesce(summary, '') <> ''
         ORDER BY subject_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def resolve_vectors(rows: list[tuple[int, str]]) -> dict[int, np.ndarray]:
    """按 subject_id 拿到向量：先查缓存，未命中的才请求 API。"""
    # ⚠️ 先按文本去重再请求 —— 缓存是按文本键的，同一段文本请求两次是白花钱。
    uniq = sorted({text for _, text in rows})

    cache = embed_cache.connect()
    try:
        hit = embed_cache.get_many(cache, uniq)
        miss = [t for t in uniq if t not in hit]
        n_cached, mb = embed_cache.stats(cache)
        print(f"缓存：命中 {len(hit)} / {len(uniq)}，待请求 {len(miss)}"
              f"（缓存库现有 {n_cached} 条 / {mb:.1f} MB）")

        if miss:
            est_tokens = int(sum(len(t) for t in miss) * TOKENS_PER_CHAR)
            print(f"预计 ≈ {est_tokens:,} token ≈ ¥{est_tokens / 1e6 * 0.07:.3f}")

            key = embed.api_key()
            budget = TokenBudget()
            for i in tqdm(range(0, len(miss), embed.MAX_BATCH), desc="请求 API"):
                batch = miss[i:i + embed.MAX_BATCH]
                budget.take(int(sum(len(t) for t in batch) * TOKENS_PER_CHAR))
                vecs = embed.embed_documents(batch, key)
                # ⚠️ 每批立刻落缓存并提交 —— 续传的粒度就是提交的粒度。
                #    攒到最后一次性写，中途挂掉就等于没跑。
                embed_cache.put_many(cache, list(zip(batch, vecs, strict=True)))
                hit.update(dict(zip(batch, vecs, strict=True)))
    finally:
        cache.close()

    return {sid: hit[text] for sid, text in rows if text in hit}


def write_vectors(conn, vectors: dict[int, np.ndarray]) -> None:
    """写 vec 列。⚠️ 用 HalfVector 显式包装。

    numpy 数组直接传也能work（psycopg 转成 vector 文本再让 PG 转型），
    但那是隐式适配，pgvector 版本一变就可能换行为。显式的更稳。
    """
    items = [(HalfVector(v), sid) for sid, v in vectors.items()]
    with conn.cursor() as cur:
        for i in range(0, len(items), 500):
            cur.executemany(
                "UPDATE anime_profile SET vec = %s, updated_at = now() WHERE subject_id = %s",
                items[i:i + 500],
            )
            conn.commit()


def clear_empty(conn) -> int:
    """summary 为空却有 vec 的，清成 NULL。

    ⚠️ 保证不变式「vec IS NOT NULL ⟺ summary 非空」。不做的话，
       某部作品的 summary 被后续同步清空后会留下一个陈旧向量，
       而打分不会报错，只会对它静默失准。
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE anime_profile SET vec = NULL
             WHERE coalesce(summary, '') = '' AND vec IS NOT NULL
        """)
        n = cur.rowcount
    conn.commit()
    return n


def record_meta(conn, rows: int) -> None:
    """登记出处。⚠️ 没有这一行就说明本脚本没跑完 —— 而半灌的 vec 比全空更危险。"""
    from datetime import UTC, datetime

    desc = embed.descriptor(
        rows=rows, built_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO build_meta (key, value, updated_at)
                 VALUES ('embed_vec', %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (Json(desc),))
    conn.commit()
    print(f"build_meta['embed_vec'] = {desc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="只处理前 N 部（小样本试跑）")
    args = ap.parse_args()

    # ⚠️ **三段式：读 → 请求 API → 写，每段各开各的连接。**
    #    2026-08-14 实测教训：原先是「开一个连接从头用到尾」，结果 API 阶段
    #    跑了 11 分 44 秒，等轮到写库时连接已经被 Neon 回收 ——
    #    `SSL connection has been closed unexpectedly`，10,864 条向量算完却写不进去。
    #    ⚠️ Neon 是 serverless，**空闲连接会被回收**，不能跨长耗时阶段持有。
    #    50 条的小样本只跑 4 秒，暴露不出这个问题。

    # ── 第 1 段：前置检查 + 取文本 ──────────────────────────────
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                 WHERE attrelid = 'anime_profile'::regclass AND attname = 'vec'
            """)
            col_type = cur.fetchone()[0]
        if col_type != f"halfvec({embed.DIM})":
            print(f"✗ anime_profile.vec 是 {col_type}，应为 halfvec({embed.DIM})。"
                  f"先跑 sql/003_vec_halfvec.sql", file=sys.stderr)
            return 1

        # ⚠️ build_meta 的检查必须**前置**。record_meta() 是脚本最后一步，
        #    表不存在的话会在跑完全量、花完钱之后才报错 —— 与上面的 halfvec
        #    检查不对称。所有前置条件都要在花钱之前查完。
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('build_meta')")
            if cur.fetchone()[0] is None:
                print("✗ build_meta 表不存在。先跑 sql/004_build_meta.sql",
                      file=sys.stderr)
                return 1

        rows = fetch_rows(conn, args.limit)

    print(f"待处理 {len(rows)} 部（指纹 {embed.fingerprint()}）")
    if not rows:
        print("✗ 没有非空 summary，先跑 scripts/load_profiles.py", file=sys.stderr)
        return 1

    # ── 第 2 段：请求 API（耗时最长，期间**不持有** DB 连接）──────
    vectors = resolve_vectors(rows)

    # ── 第 3 段：重新连库写入 ───────────────────────────────────
    with db.connect() as conn:
        print(f"写库 {len(vectors)} 行…")
        write_vectors(conn, vectors)

        if not args.limit:      # 小样本试跑时不动全表的不变式
            cleared = clear_empty(conn)
            if cleared:
                print(f"清掉 {cleared} 行「summary 空却有 vec」的陈旧向量")
            record_meta(conn, len(vectors))

        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FILTER (WHERE vec IS NOT NULL), count(*)
                  FROM anime_profile
            """)
            with_vec, total = cur.fetchone()
        print(f"\n✓ anime_profile.vec：{with_vec} / {total} 非空")

    if not args.limit:
        print("⚠️ 接下来跑 `psql -c 'VACUUM FULL anime_profile'` 回收 MVCC 膨胀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

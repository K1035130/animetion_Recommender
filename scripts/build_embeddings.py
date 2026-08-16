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
    uv run --group etl python scripts/build_embeddings.py -c 12        # 想更快
    psql -c 'VACUUM FULL anime_profile'                                # ⚠️ 别漏

⚠️ **先跑 --limit 50 看一遍**（第 15 节原则 5）。全量 ¥0.14，
   不贵，但字段理解错了的话小样本几秒就能发现。

成本与耗时：11,453 部里非空 summary 约 10,864 条 ≈ 200 万 token ≈ ¥0.14。

⚠️ **瓶颈换过两次，别照抄旧数字：**

     串行时代（~2026-08-14）  批数 × 单批往返   340 批 × 2.2s = 11 分 44 秒（实测）
     并发之后（2026-08-15）   TokenBudget       2.81M 估算 token ÷ 800k/min ≈ 3.5 分钟

   A.7 按 TPM 估的「2 分钟」当时是错的（那时串行，真实 11 分 44 秒）；
   加了 8 路并发之后请求延迟不再是瓶颈（实测 1.44 → 0.29 s/批，**提速 4.9×**），
   于是 TPM 预算重新成为约束 —— 这次它才是对的。

⚠️ **所以再加路数没用**：8 路已经够把延迟压到预算线以下，
   再多只是更早撞上 TokenBudget 然后干等。要更快得先动 TOKENS_PER_MIN，
   而那是限流条款不是我们能调的。
⚠️ 成本一分不变 —— 并发只是把等待重叠，token 总量不受影响。

⚠️ 重跑时命中缓存的部分不花钱也不花时间。
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# 并发路数。⚠️ **并发在这里几乎是免费的**：RPM 2,000（≈33 req/s）离用满差两个
# 数量级，而 TPM 由 TokenBudget 管着 —— 并发只是把等待重叠，总 token 不变，不更贵。
#
# 2026-08-15 实测（256 条 / 8 批，临时缓存）：
#     1 路   11.5s   1.44 s/批
#     8 路    2.3s   0.29 s/批     → 4.9×
#
# ⚠️ **默认 8 不是随便取的：再往上加基本没有收益。** 8 路已经把请求延迟压到
#    TokenBudget 的放行速度以下，瓶颈已经从「等 API 返回」换成「等预算」，
#    加到 16 只是更早撞预算然后干等。
DEFAULT_CONCURRENCY = 8

# ⚠️ 上限不是性能考虑，是**别把公共服务当压测目标**。
MAX_CONCURRENCY = 16

# ⚠️ 非 TTY（重定向到日志文件）时必须压低刷新频率 —— 与 scripts/fetch_moegirl.py
#    同一条理由（CLAUDE.md E.7c）：tqdm 在非 TTY 下每次刷新都**追加**一段输出，
#    而 set_postfix_str 每次调用都触发刷新，几百批能刷出几千行。
TTY = sys.stderr.isatty()
BAR_INTERVAL = 0.5 if TTY else 30.0


def make_bar(total: int, desc: str, unit: str) -> tqdm:
    return tqdm(total=total, desc=desc, unit=unit, ascii=True, ncols=78,
                mininterval=BAR_INTERVAL)


class TokenBudget:
    """按 token 的滑动窗口节流。**线程安全** —— 所有并发路共用同一份预算。

    ⚠️ 共用是必须的：每路各持一份 TOKENS_PER_MIN 的预算，8 路就是 8 倍超发，
       等于没有节流。
    """

    def __init__(self, per_min: int = TOKENS_PER_MIN) -> None:
        self.per_min = per_min
        self.window: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def take(self, tokens: int) -> None:
        """预约 tokens 的额度，不够就等到够为止。

        ⚠️ **绝不能持锁 sleep。** 那会让所有线程排在锁上等，整个池退化成串行
           —— 表现是「加了并发但一点没变快」，而且不报任何错。
           锁只保护 window 的读改写，睡在锁外面。
        """
        while True:
            with self._lock:
                now = time.monotonic()
                while self.window and now - self.window[0][0] > 60.0:
                    self.window.popleft()
                used = sum(t for _, t in self.window)
                if not self.window or used + tokens <= self.per_min:
                    self.window.append((now, tokens))
                    return
                sleep = 60.0 - (now - self.window[0][0])
            # ⚠️ jitter：不加的话所有线程会在同一时刻醒来抢同一份额度，
            #    形成同步的惊群 —— 一个抢到，其余全部空转再睡，反复如此。
            time.sleep(max(sleep, 0.0) + random.uniform(0, 0.25))


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


def run_batches(cache, hit: dict[str, np.ndarray],
                batches: list[list[str]], concurrency: int) -> None:
    """并发请求 API，结果就地写进 cache 和 hit。

    ⚠️ **SQLite 的写入全部发生在主线程** —— 工作线程只负责发请求、返回向量，
       落盘由主线程在消费 future 时做。串行化因此是**构造上的**，不靠锁：
       sqlite3 的连接默认 check_same_thread=True，跨线程用会直接抛异常；
       就算关掉那个开关，多线程并发 commit 也会撞 `database is locked`。

    ⚠️ **并发不改变批次组成，只改变发出顺序。** batches 是对排好序的 miss
       固定切片得到的，与路数无关；而缓存键是「模型+维度+文本」，跟批次无关。
       ⇒ 换个并发数重跑，缓存命中情况完全一致，可复现性不受影响。
    """
    key = embed.api_key()
    budget = TokenBudget()
    stop = threading.Event()

    def work(batch: list[str]) -> np.ndarray | None:
        # ⚠️ 两处都要查 stop：等预算可能一等就是几十秒，
        #    期间别的线程可能已经因额度耗尽喊停了。
        if stop.is_set():
            return None
        budget.take(int(sum(len(t) for t in batch) * TOKENS_PER_CHAR))
        if stop.is_set():
            return None
        return embed.embed_documents(batch, key)

    bar = make_bar(len(batches), f"请求 API ×{concurrency}", "批")
    done = 0
    pool = ThreadPoolExecutor(max_workers=concurrency)
    futures = {pool.submit(work, b): b for b in batches}
    try:
        for fut in as_completed(futures):
            batch = futures[fut]
            vecs = fut.result()          # 工作线程里的异常在这里重新抛出
            if vecs is None:             # 被 stop 掐掉的
                continue
            # ⚠️ 每批立刻落缓存并提交 —— 续传的粒度就是提交的粒度。
            #    攒到最后一次性写，中途挂掉就等于没跑。
            embed_cache.put_many(cache, list(zip(batch, vecs, strict=True)))
            hit.update(dict(zip(batch, vecs, strict=True)))
            done += len(batch)
            bar.update(1)
            bar.set_postfix_str(f"{done:,} 条", refresh=False)
    except BaseException:
        # ⚠️ QuotaExhausted 或 Ctrl-C：**立刻停发**，不要让排队中的几百批继续打。
        #    cancel_futures 清掉还没开跑的，stop 让已出队的空转返回。
        stop.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        bar.close()
        pool.shutdown(wait=True)
        embed.close_client()             # 长耗时阶段结束，放掉连接池


def resolve_texts(texts: list[str],
                  concurrency: int = DEFAULT_CONCURRENCY) -> dict[str, np.ndarray]:
    """文本 → 向量：先查缓存，未命中的才请求 API。

    ⚠️ **这一层按文本键，不认识 subject_id** —— 因为它有第二个调用方
       （scripts/build_plot_chunks.py 灌的是 chunk，没有 subject_id）。
       把缓存查询、去重、节流、并发都收在这里，两个脚本就不可能用不同的速率
       打同一个 API。分叉了不会报错，只会偶发限流 —— 那种 bug 最难查。
    """
    # ⚠️ 先按文本去重再请求 —— 缓存是按文本键的，同一段文本请求两次是白花钱。
    #    chunk 语料里实测有 12 条完全重复的文本，profile 那边有 128 条。
    uniq = sorted(set(texts))

    cache = embed_cache.connect()
    try:
        hit = embed_cache.get_many(cache, uniq)
        miss = [t for t in uniq if t not in hit]
        n_cached, mb = embed_cache.stats(cache)
        print(f"缓存：命中 {len(hit)} / {len(uniq)}，待请求 {len(miss)}"
              f"（缓存库现有 {n_cached} 条 / {mb:.1f} MB）")

        if miss:
            est_tokens = int(sum(len(t) for t in miss) * TOKENS_PER_CHAR)
            batches = [miss[i:i + embed.MAX_BATCH]
                       for i in range(0, len(miss), embed.MAX_BATCH)]
            print(f"预计 ≈ {est_tokens:,} token ≈ ¥{est_tokens / 1e6 * 0.07:.3f}"
                  f"，{len(batches)} 批 / {concurrency} 路并发")
            run_batches(cache, hit, batches, concurrency)
    finally:
        cache.close()

    return hit


def resolve_vectors(rows: list[tuple[int, str]],
                    concurrency: int = DEFAULT_CONCURRENCY) -> dict[int, np.ndarray]:
    """按 subject_id 拿到向量。只是 resolve_texts 的一层键映射。"""
    hit = resolve_texts([text for _, text in rows], concurrency)
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
    ap.add_argument("-c", "--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"并发路数（1–{MAX_CONCURRENCY}，默认 {DEFAULT_CONCURRENCY}）")
    args = ap.parse_args()

    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        print(f"✗ --concurrency 应在 1–{MAX_CONCURRENCY} 之间，收到 {args.concurrency}",
              file=sys.stderr)
        return 1

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
    try:
        vectors = resolve_vectors(rows, args.concurrency)
    except embed.QuotaExhausted as e:
        # ⚠️ 额度耗尽/鉴权失败不是「重试一下」能解决的（A.8），直接停。
        #    已经编码好的都已逐批落进缓存 —— 补上额度后重跑是零成本续传。
        print(f"\n✗ {e}", file=sys.stderr)
        print("  已编码的部分都在缓存里，补额度后重跑即可续传（命中的不重新花钱）",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。编码好的批次都已落缓存，重跑从断点继续。", file=sys.stderr)
        return 130

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

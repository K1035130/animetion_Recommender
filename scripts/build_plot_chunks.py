"""灌 plot_chunk —— 萌娘百科作品页语料（批次 2）。

⚠️ **职责边界**：本脚本只写 moegirl_page / plot_chunk / plot_chunk_scope 三张表
   和 build_meta 的 'plot_chunk' 一行，绝不碰 anime_profile 的任何列。
   幂等，可重复执行（靠 plot_chunk_uniq 约束做 upsert）。

前置：
  · sql/007_plot_chunk.sql
  · sql/004_build_meta.sql
  · scripts/fetch_moegirl.py + scripts/parse_moegirl.py 的产出：
        data/interim/moegirl_chunks.jsonl
        data/interim/moegirl_titles.json
  · .env 里的 SILICONFLOW_API_KEY

跑法：
    uv run --group etl python scripts/build_plot_chunks.py --limit 20   # 先小样本
    uv run --group etl python scripts/build_plot_chunks.py              # 再全量

规模与成本（2026-08-16 实测语料）：
    19,526 chunk / 2,232 页 / 404 万字 ≈ 2.83M token ≈ ¥0.20
    8 路并发下 API 阶段 ≈ 5 分钟（瓶颈是 TokenBudget 不是延迟）

⚠️ **什么时候必须重跑**：
   · 改了 parse_moegirl.py 的切分/清洗规则（chunk 文本变了 → 缓存键全变 → 重新花钱）
   · 改了 src/embed.py 的 MODEL / DIM
   · 改了 jieba 词典（search_tsv 会和查询端对不上）
   ⚠️ 只改 spoiler_level 的判定规则**不必**重跑编码 —— 原始信号
      （heimu_chars / spoiler_box）都在库里，一条 UPDATE 就能重算。
      这正是 sql/007 存那两列的理由。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

# ⚠️ 复用 build_embeddings 的编排，**不要复制一份**。
#    TokenBudget 的限额常数、退避策略、SQLite 写入串行化这些东西一旦分叉，
#    两个脚本就会用不同的速率打同一个 API —— 而分叉不报错，只会偶发限流。
#    （scripts/ 整个被 .vercelignore 排除，这个 import 不影响线上 bundle。）
from build_embeddings import make_bar, resolve_texts
from pgvector import HalfVector
from psycopg.types.json import Json

from src import db, embed
from src.textproc import dict_fingerprint, tokenize

CHUNKS = Path(__file__).resolve().parent.parent / "data" / "interim" / "moegirl_chunks.jsonl"
TITLES = Path(__file__).resolve().parent.parent / "data" / "interim" / "moegirl_titles.json"

# 写库批大小。⚠️ 别调太大：单批越大，中途失败时回滚掉的越多。
WRITE_BATCH = 1_000


# ============================================================
# 第 1 段：前置检查 + 读出库内状态
# ============================================================
def preflight(conn) -> set[int]:
    """检查表结构，返回库内所有 subject_id（用来挡 FK 违例）。"""
    with conn.cursor() as cur:
        for t in ("moegirl_page", "plot_chunk", "plot_chunk_scope"):
            cur.execute("SELECT to_regclass(%s)", (t,))
            if cur.fetchone()[0] is None:
                raise SystemExit(f"✗ 表 {t} 不存在。先跑 sql/007_plot_chunk.sql")

        cur.execute("SELECT to_regclass('build_meta')")
        if cur.fetchone()[0] is None:
            raise SystemExit("✗ build_meta 表不存在。先跑 sql/004_build_meta.sql")

        # ⚠️ 维度必须与 src/embed.py 一致。不一致的话写入会报错，
        #    但那是在花完钱之后 —— 所有前置检查都要在 API 阶段之前。
        cur.execute("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
             WHERE attrelid = 'plot_chunk'::regclass AND attname = 'vec'
        """)
        col = cur.fetchone()[0]
        if col != f"halfvec({embed.DIM})":
            raise SystemExit(f"✗ plot_chunk.vec 是 {col}，应为 halfvec({embed.DIM})")

        cur.execute("SELECT subject_id FROM anime_profile")
        return {r[0] for r in cur.fetchall()}


# ============================================================
# 第 2 段：读语料（不碰数据库）
# ============================================================
def load_corpus(known: set[int], limit: int | None):
    """返回 (pages, chunks, scope)。

    ⚠️ series_roots 里不在 anime_profile 的要**在这里挡掉**，否则写
       plot_chunk_scope 时才炸外键 —— 那时 API 的钱已经花完了。
    """
    if not CHUNKS.exists():
        raise SystemExit(f"✗ 缺少 {CHUNKS}，先跑 scripts/parse_moegirl.py")
    if not TITLES.exists():
        raise SystemExit(f"✗ 缺少 {TITLES}，先跑 scripts/fetch_moegirl.py")

    manifest = json.loads(TITLES.read_text(encoding="utf-8"))
    # manifest 的键是 subject_id，值里带 pageid —— 我们要的是按 pageid 索引
    meta = {v["pageid"]: v for v in manifest.values()}

    rows = [json.loads(x) for x in CHUNKS.read_text(encoding="utf-8").splitlines()]

    # 按页分组后再截断，保证 --limit 时页面是完整的（半页语料没有意义）
    by_page: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_page[r["pageid"]].append(r)
    pageids = sorted(by_page)
    if limit:
        pageids = pageids[:limit]

    pages, chunks, scope = [], [], set()
    dropped_roots = 0
    for pid in pageids:
        m = meta.get(pid)
        if m is None:                       # manifest 与 chunk 文件不同步
            continue
        pages.append((pid, "series", m["title"], int(m["lastrevid"])))
        for r in by_page[pid]:
            chunks.append(r)
            for sr in r["series_roots"]:
                if sr in known:
                    scope.add((pid, sr))
                else:
                    dropped_roots += 1

    if dropped_roots:
        print(f"⚠️ 有 {dropped_roots} 处 series_root 不在 anime_profile 里，已跳过")
    return pages, chunks, sorted(scope)


# ============================================================
# 第 3 段：写库
# ============================================================
UPSERT_PAGE = """
    INSERT INTO moegirl_page (pageid, kind, title, lastrevid)
         VALUES (%s, %s, %s, %s)
    ON CONFLICT (pageid) DO UPDATE
       SET kind = EXCLUDED.kind, title = EXCLUDED.title,
           lastrevid = EXCLUDED.lastrevid, fetched_at = now()
"""

# ⚠️ search_tsv 在 SQL 里 to_tsvector，但**分词在 Python 侧做**（jieba）——
#    Neon 装不了 zhparser，Postgres 内置分词器切不了中文。
#    'simple' 配置只做小写化，不做词干还原，正是我们要的。
UPSERT_CHUNK = """
    INSERT INTO plot_chunk
        (source, pageid, chunk_no, kind, section, section_id,
         text, spoiler_level, heimu_chars, spoiler_box, vec, search_tsv)
    VALUES ('moegirl', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            to_tsvector('simple', %s))
    ON CONFLICT ON CONSTRAINT plot_chunk_uniq DO UPDATE
       SET kind = EXCLUDED.kind, section = EXCLUDED.section,
           section_id = EXCLUDED.section_id, text = EXCLUDED.text,
           spoiler_level = EXCLUDED.spoiler_level,
           heimu_chars = EXCLUDED.heimu_chars, spoiler_box = EXCLUDED.spoiler_box,
           vec = EXCLUDED.vec, search_tsv = EXCLUDED.search_tsv
"""


def write_pages(conn, pages) -> None:
    with conn.cursor() as cur:
        cur.executemany(UPSERT_PAGE, pages)
    conn.commit()
    print(f"moegirl_page：{len(pages)} 页")


def existing_digests(conn) -> dict[tuple[int, int], str]:
    """库里已有 chunk 的 (pageid, chunk_no) → md5(text)。

    ⚠️ **用来跳过没变的行，这不是可选优化。** 2026-08-16 实测教训：
       增量灌库只新增了 601 条，脚本却把全部 20,127 行都 upsert 了一遍
       （n_tup_upd = 19,767），结果 heap 21→42 MB、TOAST 60→121 MB **翻倍** ——
       因为每次 UPDATE 都重写整行，连带重写行外的 halfvec（2 KB，全在 TOAST 里）。
       空间经 VACUUM 后可复用、不会无限涨，但那是白烧 Neon 的存储与 WAL。
       ⚠️ 阶段 03 是 66,871 行，同样的浪费会大三倍。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pageid, chunk_no, md5(text) FROM plot_chunk
             WHERE source = 'moegirl' AND vec IS NOT NULL AND search_tsv IS NOT NULL
        """)
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def write_chunks(conn, chunks, vectors: dict[str, np.ndarray]) -> int:
    have = existing_digests(conn)
    todo = [r for r in chunks
            if have.get((r["pageid"], r["chunk_no"]))
            != hashlib.md5(r["text"].encode("utf-8")).hexdigest()]
    skipped = len(chunks) - len(todo)
    if skipped:
        print(f"跳过 {skipped:,} 条未变化的（只写 {len(todo):,} 条）")
    chunks = todo

    missing = 0
    bar = make_bar(len(chunks), "写 plot_chunk", "条")
    with conn.cursor() as cur:
        for i in range(0, len(chunks), WRITE_BATCH):
            batch = chunks[i:i + WRITE_BATCH]
            params = []
            for r in batch:
                v = vectors.get(r["text"])
                if v is None:               # 编码阶段漏了（不该发生，但别静默）
                    missing += 1
                    continue
                params.append((
                    r["pageid"], r["chunk_no"], r["kind"],
                    r.get("section"), r.get("section_id"),
                    r["text"], r["spoiler_level"],
                    r.get("heimu_chars", 0), bool(r.get("spoiler_box")),
                    HalfVector(v),
                    tokenize(r["text"]),
                ))
            cur.executemany(UPSERT_CHUNK, params)
            conn.commit()                   # ⚠️ 每批提交：中途挂掉不用从头再来
            bar.update(len(batch))
    bar.close()
    if missing:
        print(f"⚠️ {missing} 条 chunk 没有向量，已跳过（检查编码阶段）")
    return len(chunks) - missing


def write_scope(conn, scope) -> int:
    """建作用域映射。

    ⚠️ 走临时表 + JOIN，而不是把 chunk_id 一条条传回 Python ——
       实测 series_roots 在同一页内恒定（2,232 页零例外），
       所以「页 → 系列根」这层映射足以推出「chunk → 系列根」。
    """
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_scope (pageid int, series_root int) "
                    "ON COMMIT DROP")
        cur.executemany("INSERT INTO tmp_scope VALUES (%s, %s)", scope)
        cur.execute("""
            INSERT INTO plot_chunk_scope (series_root, chunk_id)
            SELECT t.series_root, c.chunk_id
              FROM tmp_scope t
              JOIN plot_chunk c ON c.pageid = t.pageid AND c.source = 'moegirl'
            ON CONFLICT DO NOTHING
        """)
        n = cur.rowcount
    conn.commit()
    return n


def record_meta(conn, n_chunks: int, n_pages: int) -> None:
    """登记出处。⚠️ 没有这一行就说明本脚本没跑完 —— 半灌的语料比全空更危险。"""
    desc = embed.descriptor(
        table="plot_chunk", source="moegirl",
        pages=n_pages, rows=n_chunks,
        dict_fingerprint=dict_fingerprint(),
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO build_meta (key, value, updated_at)
                 VALUES ('plot_chunk', %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (Json(desc),))
    conn.commit()
    print(f"build_meta['plot_chunk'] = {desc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="只处理前 N 个页面（小样本试跑）")
    ap.add_argument("-c", "--concurrency", type=int, default=8,
                    help="embedding 并发路数（默认 8）")
    args = ap.parse_args()

    # ── 第 1 段：前置检查（短连接）─────────────────────────────
    # ⚠️ **三段式：读 → 请求 API → 写，每段各开各的连接。**
    #    Neon 是 serverless，空闲连接会被回收。第 3 周就是握着连接跑完
    #    11 分钟的 API 阶段，写库时 SSL connection has been closed unexpectedly。
    with db.connect() as conn:
        known = preflight(conn)

    pages, chunks, scope = load_corpus(known, args.limit)
    if not chunks:
        print("✗ 没有可灌的 chunk", file=sys.stderr)
        return 1
    print(f"待灌 {len(chunks):,} chunk / {len(pages):,} 页 / "
          f"{len(scope):,} 条页-系列映射")
    print(f"指纹 embed={embed.fingerprint()} · jieba={dict_fingerprint()}")

    # ── 第 2 段：编码（耗时最长，期间**不持有** DB 连接）──────
    try:
        vectors = resolve_texts([r["text"] for r in chunks], args.concurrency)
    except embed.QuotaExhausted as e:
        print(f"\n✗ {e}", file=sys.stderr)
        print("  已编码的都在缓存里，补额度后重跑即可续传", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。编码好的批次都已落缓存，重跑从断点继续。", file=sys.stderr)
        return 130

    # ── 第 3 段：重新连库写入 ───────────────────────────────
    with db.connect() as conn:
        write_pages(conn, pages)
        write_chunks(conn, chunks, vectors)
        n_scope = write_scope(conn, scope)
        print(f"plot_chunk_scope：新增 {n_scope:,} 行")
        if not args.limit:
            # ⚠️ 记**语料总量**，不是本次写入量。加了跳过逻辑之后
            #    write_chunks 返回的是「这次写了几条」，增量重跑时是 0 ——
            #    拿它去写 build_meta 会让指纹行显示 rows=0，
            #    看着像"没灌成功"，实际库里是满的。
            #    与 E.7c 记的「统计输出把条目总数写死」同类：不报错，只是数字错。
            record_meta(conn, len(chunks), len(pages))

        with conn.cursor() as cur:
            cur.execute("""
                SELECT source, kind, count(*), count(vec), count(search_tsv)
                  FROM plot_chunk GROUP BY 1, 2 ORDER BY 1, 2
            """)
            print("\n✓ plot_chunk：")
            for src, kind, c, v, t in cur.fetchall():
                print(f"    {src:14} {kind:8} {c:>7,} 条 · vec {v:>7,} · tsv {t:>7,}")
            cur.execute("SELECT count(DISTINCT series_root) FROM plot_chunk_scope")
            print(f"  覆盖系列根 {cur.fetchone()[0]:,} 个")

    # ⚠️ 用**普通 VACUUM 不是 VACUUM FULL**：后者重写整张表、产生等于表大小的
    #    WAL，而 WAL 进 Neon 的存储计量（第 3 周两次就贡献了约 150 MB）。
    #    纯 INSERT 本来也没多少死元组，重跑时的 upsert 才有。
    if not args.limit:
        with db.connect(autocommit=True) as conn, conn.cursor() as cur:
            print("\nVACUUM ANALYZE plot_chunk …")
            cur.execute("VACUUM ANALYZE plot_chunk")
            cur.execute("VACUUM ANALYZE plot_chunk_scope")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

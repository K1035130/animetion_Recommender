"""灌 plot_chunk —— Bangumi dump 的角色简介（阶段 03）。

⚠️ **职责边界**：本脚本只写 plot_chunk / plot_chunk_scope 里 `source='bangumi_char'`
   的行、alias 里 `entity_type='character'` 的行，和 build_meta 的 'char_chunk' 一行。
   **绝不碰 anime_profile 的任何列，也绝不碰别的脚本负责的 alias 行**
   （load_profiles.py 管 source in ('name','name_cn','infobox_alias') 那批，
   本脚本用 'char_*' 前缀，两者永不相交 —— C 节「各管各的列」那条铁律）。
   幂等，可重复执行。

前置：
  · sql/001_init.sql（alias 表）· sql/004_build_meta.sql · sql/007_plot_chunk.sql
  · data/raw/dump/character.jsonlines
  · data/raw/dump/subject-characters.jsonlines
  · .env 里的 SILICONFLOW_API_KEY

跑法：
    uv run --group etl python scripts/build_char_chunks.py --limit 500   # 先小样本
    uv run --group etl python scripts/build_char_chunks.py               # 再全量

规模与成本（2026-08-16 实测口径）：
    dump 全量 217,150 个角色，但**限定在我们 11,453 部之内**只有 96,112 个，
    其中 66,871 个有 summary（69.6%）· 均 139 字 · 中位 89 · p90 287 · max 6,127
    ≈ 6.5M token ≈ ¥0.46 ·  涉及作品 9,245 部 / 6,545 个 series_root
    ⚠️ 零抓取 —— 数据全在磁盘上，唯一的网络调用是 embedding API。

⚠️ **本批语料没有任何剧透信号。** dump 的角色简介里「最后死了」「其实是反派」
   这类内容大量存在，但**没有 heimu、没有剧透框**，所以 66,871 条全部
   `spoiler_level=0`。这不是漏标，是这个来源根本不带这个信号。
   ⇒ 它进一步坐实了「前端无条件加剧透提醒」那条决定（见 CLAUDE.md 第四部分），
     **不要**因为 spoiler_level=0 就认为这批内容安全。

⚠️ **什么时候必须重跑**：
   · 换了 dump（角色 summary 变了 → 缓存键变 → 重新花钱）
   · 改了 src/embed.py 的 MODEL / DIM
   · 改了 jieba 词典（search_tsv 会和查询端对不上）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

# ⚠️ 三个复用，**都不要复制一份实现**：
#    · resolve_texts —— TokenBudget 限额 / 退避 / SQLite 串行化。分叉就会用不同
#      速率打同一个 API，而分叉不报错，只偶发限流。
#    · chunk_blocks —— 切分参数必须与萌娘语料**逐字一致**：两个来源共用一张表、
#      同一个向量空间，粒度不同会让检索时长短 chunk 系统性不可比。
#    · infobox_aliases —— 用官方 wiki parser，不自己写正则。
from build_embeddings import make_bar, resolve_texts
from load_profiles import infobox_aliases
from parse_moegirl import chunk_blocks
from pgvector import HalfVector
from psycopg.types.json import Json

from src import db, embed, langclean
from src.textproc import dict_fingerprint, norm_name, tokenize

DUMP = Path(__file__).resolve().parent.parent / "data" / "raw" / "dump"
CHARACTERS = DUMP / "character.jsonlines"
SUBJECT_CHARACTERS = DUMP / "subject-characters.jsonlines"

WRITE_BATCH = 1_000

# alias 里属于本脚本的 source 值。⚠️ 与 load_profiles.py 的三个值刻意不重名。
ALIAS_SOURCES = ("char_name", "char_name_cn", "char_alias")

# infobox 里的中文名字段。character 的 infobox 是 {{Infobox Crt}}，
# 没有顶层 name_cn（那是 subject 才有的），中文名只在这里。
CN_NAME_KEYS = ("简体中文名", "中文名")


# ============================================================
# 第 1 段：前置检查 + 读出库内状态
# ============================================================
def preflight(conn) -> dict[int, int]:
    """检查表结构，返回 {subject_id: series_root}。

    ⚠️ 所有检查都必须在 API 阶段**之前** —— 花完钱才发现表不对是最蠢的失败。
    """
    with conn.cursor() as cur:
        for t in ("plot_chunk", "plot_chunk_scope", "alias", "build_meta"):
            cur.execute("SELECT to_regclass(%s)", (t,))
            if cur.fetchone()[0] is None:
                raise SystemExit(f"✗ 表 {t} 不存在。先跑 sql/001 + sql/004 + sql/007")

        cur.execute("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
             WHERE attrelid = 'plot_chunk'::regclass AND attname = 'vec'
        """)
        col = cur.fetchone()[0]
        if col != f"halfvec({embed.DIM})":
            raise SystemExit(f"✗ plot_chunk.vec 是 {col}，应为 halfvec({embed.DIM})")

        # ⚠️ series_root 必须已经灌好。它全表非空，为空说明 build_tag_vectors.py 没跑，
        #    那样 scope 会整片建不出来 —— 而且不报错，只是检索时什么都搜不到。
        cur.execute("SELECT subject_id, series_root FROM anime_profile")
        root = {s: r for s, r in cur.fetchall()}
        missing = sum(1 for v in root.values() if v is None)
        if missing:
            raise SystemExit(
                f"✗ 有 {missing} 部作品的 series_root 为空。先跑 build_tag_vectors.py")
        return root


# ============================================================
# 第 2 段：读 dump（不碰数据库、不联网）
# ============================================================
def char_names(rec: dict) -> list[tuple[str, str, str]]:
    """产出 (原始名, norm_name, source)，已按 norm_name 去重。"""
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(raw: str, source: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        key = norm_name(raw)
        if not key or key in seen:
            return
        seen.add(key)
        rows.append((raw, key, source))

    add(rec.get("name") or "", "char_name")
    info = rec.get("infobox") or ""
    for k in CN_NAME_KEYS:
        m = re.search(rf"\|\s*{k}\s*=\s*([^\r\n|}}]+)", info)
        if m:
            add(m.group(1), "char_name_cn")
            break
    for a in infobox_aliases(info):
        add(a, "char_alias")
    return rows


def is_name_echo(summary: str, names: list[tuple[str, str, str]]) -> bool:
    """简介只是把名字复读一遍 → 无信息量，丢掉。

    ⚠️ **判据不能用长度。** E.8 那条教训在这里同样成立：
       「神官关姬塔的侍从。」只有 9 字却是有用的角色描述，
       而「守本叶鸣」4 字是纯粹的名字复读。**短不等于没用，没用的是复读。**
       实测：按名字复读判，66,871 条里只命中 111 条（0.2%），
       且抽查全部确为噪声（守本叶鳴→守本叶鸣、勇太の母→勇太の母親）。
       若改用「<10 字」当判据，会连带杀掉 1,900 多条有信息的短简介。
    """
    ns = norm_name(summary)
    if not ns:
        return True
    keys = [k for _, k, _ in names if k]
    if ns in keys:
        return True
    # 极短的简介若被名字包含（或包含名字），同样视为复读
    return len(ns) <= 12 and any(ns == k or ns in k or k in ns for k in keys)


def load_corpus(root: dict[int, int], limit: int | None):
    """返回 (chunks, scope, aliases)。

    chunks : [{character_id, chunk_no, text}]
    scope  : [(character_id, series_root)]
    aliases: [(name, norm_name, character_id, parent_subject_id, source)]
    """
    for p in (CHARACTERS, SUBJECT_CHARACTERS):
        if not p.exists():
            raise SystemExit(f"✗ 缺少 {p}")

    # ── 角色 → 它出现在哪些库内作品 ──────────────────────────
    # ⚠️ 只保留库内的 subject。dump 里 43.9 万条关系，我们只要落在
    #    11,453 部之内的那 15.6 万条 —— 否则 scope 会撞外键，
    #    而那是在 API 花完钱之后才炸。
    links: dict[int, set[int]] = defaultdict(set)
    with SUBJECT_CHARACTERS.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["subject_id"] in root:
                links[d["character_id"]].add(d["subject_id"])

    chunks: list[dict] = []
    scope: set[tuple[int, int]] = set()
    aliases: list[tuple[str, str, int, int, str]] = []
    n_chars = n_echo = n_nosum = n_kept = 0

    with CHARACTERS.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            cid = int(d["id"])
            sids = links.get(cid)
            if not sids:
                continue
            n_chars += 1

            # ⚠️ 剥「[简介原文] + 日文」的尾巴。**必须在切块之前** ——
            #    切完再剥的话，日文会自成一条 chunk 而不是尾巴，剥不掉。
            #    也**必须在这里而不是事后 UPDATE**：本脚本从 dump 重读文本，
            #    一次性 UPDATE 会被下次重跑覆盖回去（见 src/langclean.py 顶部）。
            summary = langclean.strip_jp_tail((d.get("summary") or "").strip())
            if not summary:
                n_nosum += 1
                continue

            names = char_names(d)
            if is_name_echo(summary, names):
                n_echo += 1
                continue

            # ⚠️ 复用萌娘那套切分：97.4% 的简介 ≤600 字会原样成为一条，
            #    超长的 2.6% 被切开 —— 而它们占了总字数的 18.5%，
            #    不切的话（max 6,127 字）会在向量里稀释成一团噪声。
            made = chunk_blocks([(summary, 0, False)])
            for i, (text, _hm, _box) in enumerate(made):
                chunks.append({"character_id": cid, "chunk_no": i, "text": text})

            roots = {root[s] for s in sids}
            for r in roots:
                scope.add((cid, r))

            # ⚠️ parent_subject_id 只能存**一个**值：alias_uniq 是
            #    (entity_type, subject_id, character_id, norm_name)，
            #    同一角色同一名字只允许一行。取 min 保证确定性（可复现）。
            #    📌 **角色→多作品的完整映射在 plot_chunk_scope 里**，
            #       这一列只是给消歧用的锚点，不是权威来源。
            anchor = root[min(sids)]
            for raw, key, src in names:
                aliases.append((raw, key, cid, anchor, src))

            n_kept += 1
            if limit and n_kept >= limit:
                break

    print(f"库内角色 {n_chars:,} · 无 summary {n_nosum:,} · 名字复读丢弃 {n_echo:,}")
    return chunks, sorted(scope), aliases


# ============================================================
# 第 3 段：写库
# ============================================================
UPSERT_CHUNK = """
    INSERT INTO plot_chunk
        (source, character_id, chunk_no, kind, section,
         text, spoiler_level, heimu_chars, spoiler_box, vec, search_tsv)
    VALUES ('bangumi_char', %s, %s, 'profile', NULL,
            %s, 0, 0, false, %s, to_tsvector('simple', %s))
    ON CONFLICT ON CONSTRAINT plot_chunk_uniq DO UPDATE
       SET text = EXCLUDED.text, vec = EXCLUDED.vec,
           search_tsv = EXCLUDED.search_tsv
"""

INSERT_ALIAS = """
    INSERT INTO alias (name, norm_name, entity_type, character_id,
                       parent_subject_id, source)
         VALUES (%s, %s, 'character', %s, %s, %s)
    ON CONFLICT ON CONSTRAINT alias_uniq DO NOTHING
"""


def existing_digests(conn) -> dict[tuple[int, int], str]:
    """库里已有的 (character_id, chunk_no) → md5(text)，用来跳过没变的行。

    ⚠️ **不是可选优化。** F.4 ① 实测：增量灌库只新增 601 条，却把全部 20,127 行
       都 upsert 了一遍，heap 21→42 MB、TOAST 60→121 MB **翻倍** ——
       每次 UPDATE 都重写整行，连带重写行外的 halfvec（2 KB，全在 TOAST 里）。
       ⚠️ 本阶段是 66,871 行，同样的浪费会**大三倍**。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT character_id, chunk_no, md5(text) FROM plot_chunk
             WHERE source = 'bangumi_char'
               AND vec IS NOT NULL AND search_tsv IS NOT NULL
        """)
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def write_chunks(conn, chunks, vectors: dict[str, np.ndarray]) -> int:
    have = existing_digests(conn)
    todo = [r for r in chunks
            if have.get((r["character_id"], r["chunk_no"]))
            != hashlib.md5(r["text"].encode("utf-8")).hexdigest()]
    skipped = len(chunks) - len(todo)
    if skipped:
        print(f"跳过 {skipped:,} 条未变化的（只写 {len(todo):,} 条）")

    missing = 0
    bar = make_bar(len(todo), "写 plot_chunk", "条")
    with conn.cursor() as cur:
        for i in range(0, len(todo), WRITE_BATCH):
            batch = todo[i:i + WRITE_BATCH]
            params = []
            for r in batch:
                v = vectors.get(r["text"])
                if v is None:
                    missing += 1
                    continue
                params.append((r["character_id"], r["chunk_no"], r["text"],
                               HalfVector(v), tokenize(r["text"])))
            cur.executemany(UPSERT_CHUNK, params)
            conn.commit()               # ⚠️ 每批提交：中途挂掉不用从头再来
            bar.update(len(batch))
    bar.close()
    if missing:
        print(f"⚠️ {missing} 条没有向量，已跳过（检查编码阶段）")
    return len(todo) - missing


def write_scope(conn, scope) -> int:
    """建作用域映射。走临时表 + JOIN，不把 chunk_id 一条条传回 Python。"""
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_cscope (character_id int, series_root int) "
                    "ON COMMIT DROP")
        cur.executemany("INSERT INTO tmp_cscope VALUES (%s, %s)", scope)
        cur.execute("""
            INSERT INTO plot_chunk_scope (series_root, chunk_id)
            SELECT t.series_root, c.chunk_id
              FROM tmp_cscope t
              JOIN plot_chunk c ON c.character_id = t.character_id
                               AND c.source = 'bangumi_char'
            ON CONFLICT DO NOTHING
        """)
        n = cur.rowcount
    conn.commit()
    return n


def write_aliases(conn, aliases) -> int:
    """填 alias 的角色行 —— 空了三周的那一块。

    📌 第 1 周就为它留好了 parent_subject_id 列（DDL 注释：「角色消歧必须锚定在
       已确认的 subject 范围内」），但一直没有写入方。实测灌之前那张表
       38,378 行**全是 entity_type='subject'**，角色行一条没有。
    ⚠️ 没有它，「吉尔伽美什最后怎样」路由不到角色，
       而主角重名 6.4%（アリス×9）在没有作品锚定时无解。
    """
    n = 0
    bar = make_bar(len(aliases), "写 alias", "行")
    with conn.cursor() as cur:
        for i in range(0, len(aliases), WRITE_BATCH):
            batch = aliases[i:i + WRITE_BATCH]
            cur.executemany(INSERT_ALIAS, batch)
            n += cur.rowcount
            conn.commit()
            bar.update(len(batch))
    bar.close()
    return n


def record_meta(conn, n_chunks: int, n_chars: int) -> None:
    """登记出处。⚠️ 没有这一行就说明本脚本没跑完 —— 半灌的语料比全空更危险。"""
    desc = embed.descriptor(
        table="plot_chunk", source="bangumi_char",
        characters=n_chars, rows=n_chunks,
        dict_fingerprint=dict_fingerprint(),
        built_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO build_meta (key, value, updated_at)
                 VALUES ('char_chunk', %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """, (Json(desc),))
    conn.commit()
    print(f"build_meta['char_chunk'] = {desc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="只处理前 N 个角色（小样本试跑）")
    ap.add_argument("-c", "--concurrency", type=int, default=8,
                    help="embedding 并发路数（默认 8）")
    # ⚠️ alias 角色行会让该表从 3.8 万涨到 23.5 万（+50 MB），而 /api/search 的
    #    trgm 兜底走**顺序扫描**（idx_alias_trgm 是第 1 周为省 9.9 MB 主动砍掉的）——
    #    堆扫描量因此涨 6 倍。正确性不受影响（那条查询有 entity_type='subject'
    #    过滤 + subject_id JOIN 双重保护），但延迟要实测。
    #    ⇒ 留一个开关，让「灌语料」和「灌别名」可以分开决策。
    #    💡 之后单独补跑 alias 只要不带这个 flag：chunk 走 md5 全跳过、
    #       scope 走 ON CONFLICT DO NOTHING，实际只会写 alias。
    ap.add_argument("--no-alias", action="store_true",
                    help="不写 alias 的角色行（语料照灌）")
    args = ap.parse_args()

    # ── 第 1 段：前置检查（短连接）─────────────────────────────
    # ⚠️ **三段式：读 → 请求 API → 写，每段各开各的连接。**
    #    Neon 是 serverless，空闲连接会被回收。第 3 周就是握着连接跑完
    #    11 分钟的 API 阶段，写库时 SSL connection has been closed unexpectedly。
    with db.connect() as conn:
        root = preflight(conn)

    chunks, scope, aliases = load_corpus(root, args.limit)
    if not chunks:
        print("✗ 没有可灌的 chunk", file=sys.stderr)
        return 1
    n_chars = len({c["character_id"] for c in chunks})
    print(f"待灌 {len(chunks):,} chunk / {n_chars:,} 个角色 / "
          f"{len(scope):,} 条角色-系列映射 / {len(aliases):,} 条别名")
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
        write_chunks(conn, chunks, vectors)
        print(f"plot_chunk_scope：新增 {write_scope(conn, scope):,} 行")
        if args.no_alias:
            print(f"alias：跳过（--no-alias），{len(aliases):,} 条别名未写入")
        else:
            print(f"alias：新增 {write_aliases(conn, aliases):,} 行")
        if not args.limit:
            # ⚠️ 记**语料总量**，不是本次写入量 —— 增量重跑时后者是 0，
            #    会让指纹行显示 rows=0，看着像没灌成功而库里是满的（F.4 ②）。
            record_meta(conn, len(chunks), n_chars)

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
            cur.execute("SELECT entity_type, count(*) FROM alias GROUP BY 1 ORDER BY 1")
            print("  alias：" + " · ".join(f"{t} {n:,}" for t, n in cur.fetchall()))

    # ⚠️ 普通 VACUUM，**不是 VACUUM FULL** —— 后者重写整张表、产生等于表大小的
    #    WAL，而 WAL 进 Neon 的存储计量（第 3 周两次就贡献了约 150 MB）。
    if not args.limit:
        with db.connect(autocommit=True) as conn, conn.cursor() as cur:
            print("\nVACUUM ANALYZE …")
            cur.execute("VACUUM ANALYZE plot_chunk")
            cur.execute("VACUUM ANALYZE plot_chunk_scope")
            if not args.no_alias:
                cur.execute("VACUUM ANALYZE alias")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

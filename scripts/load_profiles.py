"""批次 1 ETL：把 Bangumi dump 的候选集灌进 anime_profile + alias。

用法：
    # 先跑 50 部端到端，人工看一遍（CLAUDE.md 第 13 节「执行建议」）
    PYTHONIOENCODING=utf-8 uv run python scripts/load_profiles.py --sample 50

    # 确认无误后全量
    PYTHONIOENCODING=utf-8 uv run python scripts/load_profiles.py

设计要点（都来自 CLAUDE.md，改之前先回去看）：
  · 候选集口径只从 src/candidates.py 取，这里不重写任何筛选逻辑
  · tag 走 normalize() + classify()，只有 KEEP 类进 tags 列
  · search_tsv 必须在 Python 侧用 jieba 预分词 —— Neon 装不了 zhparser
  · 向量列 vec、cluster_id、AniList 那几列本周留空

⚠️ 幂等性：upsert **只覆盖 dump 派生的列**。
   anilist_id / name_en / studios / staff / popularity / external_ids /
   vec / cluster_id 全部不在 UPDATE SET 里 —— 否则动作 4 补完 AniList
   之后重跑一次 ETL 就把补的数据洗掉了，而且不会报错。
"""

import argparse
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import bgm_tv_wiki
import psycopg
from psycopg.types.json import Jsonb
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import candidates, db, tag_rules
from src.textproc import dict_fingerprint, keep_tags, norm_name, tokenize

BATCH = 500

# alias 表里由 dump 派生的来源。重跑时先按这几个 source 删再插，
# 这样条目改名后旧别名不会留在库里（AniList/萌娘百科来源的行不受影响）。
DUMP_ALIAS_SOURCES = ("name", "name_cn", "infobox_alias")

UPSERT_PROFILE = """
INSERT INTO anime_profile (
    subject_id, name, name_cn, summary,
    air_date, air_year, form, platform, nsfw,
    score, score_count, score_details, rank,
    fav_done, favorite, tags, meta_tags, search_tsv
) VALUES (
    %(subject_id)s, %(name)s, %(name_cn)s, %(summary)s,
    %(air_date)s, %(air_year)s, %(form)s, %(platform)s, %(nsfw)s,
    %(score)s, %(score_count)s, %(score_details)s, %(rank)s,
    %(fav_done)s, %(favorite)s, %(tags)s, %(meta_tags)s,
    setweight(to_tsvector('simple', %(tsv_title)s), 'A') ||
    setweight(to_tsvector('simple', %(tsv_tags)s),  'B') ||
    setweight(to_tsvector('simple', %(tsv_sum)s),   'C')
)
ON CONFLICT (subject_id) DO UPDATE SET
    name          = EXCLUDED.name,
    name_cn       = EXCLUDED.name_cn,
    summary       = EXCLUDED.summary,
    air_date      = EXCLUDED.air_date,
    air_year      = EXCLUDED.air_year,
    form          = EXCLUDED.form,
    platform      = EXCLUDED.platform,
    nsfw          = EXCLUDED.nsfw,
    score         = EXCLUDED.score,
    score_count   = EXCLUDED.score_count,
    score_details = EXCLUDED.score_details,
    rank          = EXCLUDED.rank,
    fav_done      = EXCLUDED.fav_done,
    favorite      = EXCLUDED.favorite,
    tags          = EXCLUDED.tags,
    meta_tags     = EXCLUDED.meta_tags,
    search_tsv    = EXCLUDED.search_tsv,
    updated_at    = now()
"""

INSERT_ALIAS = """
INSERT INTO alias (name, norm_name, entity_type, subject_id, source)
VALUES (%s, %s, 'subject', %s, %s)
ON CONFLICT ON CONSTRAINT alias_uniq DO NOTHING
"""

DELETE_ALIAS = """
DELETE FROM alias
WHERE subject_id = ANY(%s) AND source = ANY(%s)
"""


# ------------------------------------------------------------------ 字段映射

def clean_tags(raw: list[dict] | None, stats: Counter) -> list[dict[str, Any]]:
    """用户 tag → 清洗后的题材 tag。

    两道关：
      ① classify() 把公司/人名/年份/IP/形态/地区/元评价分流出去
      ② 只保留 418 个词表内的 tag

    第 ② 关是必要的：classify() 的人工清单是在 df>=8 的口径下枚举的，
    长尾里还有大量没被枚举到的公司名和人名会从启发式漏下来判成 KEEP。
    tags 列存在的意义就是给 P0 算余弦，混进人名会直接压低上限。
    词表本身可以随时调 min_df 重建，重跑 ETL 是幂等的。
    """
    merged: Counter = Counter()
    vocab = keep_tags()
    for t in raw or []:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        canon = tag_rules.normalize(name)          # 同义合并：漫改 → 漫画改
        kind = tag_rules.classify(canon)
        if kind != "KEEP":
            stats[f"drop_{kind}"] += 1
            continue
        if canon not in vocab:
            stats["drop_LONGTAIL"] += 1
            continue
        # 同义词合并后可能撞车（漫画改 + 漫改 同时存在），票数要相加
        merged[canon] += int(t.get("count") or 0)
        stats["keep"] += 1
    return [{"name": n, "count": c} for n, c in merged.most_common()]


def infobox_aliases(infobox: str) -> list[str]:
    """从 infobox 的「别名」字段取别名。

    实测该字段是 tuple[Item]，每个 Item 的 .value 才是别名字符串
    （EVA 有 7 个，含 'Neon Genesis Evangelion' 等英文/罗马音）。
    用官方 parser 解析，不自己写 wiki 语法正则。
    """
    if not infobox:
        return []
    try:
        wiki = bgm_tv_wiki.parse(infobox)
    except bgm_tv_wiki.WikiSyntaxError:
        return []
    out: list[str] = []
    for field in wiki.fields:
        if field.key != "别名":
            continue
        val = field.value
        if isinstance(val, str):
            out.append(val)
        elif isinstance(val, tuple):
            out.extend(item.value for item in val if isinstance(item.value, str))
    return [s.strip() for s in out if s and s.strip()]


def build_aliases(rec: dict) -> list[tuple[str, str, int, str]]:
    """产出 (name, norm_name, subject_id, source) 四元组，已按 norm_name 去重。"""
    sid = rec["id"]
    rows: list[tuple[str, str, int, str]] = []
    seen: set[str] = set()

    def add(raw: str, source: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        key = norm_name(raw)
        if not key or key in seen:      # 归一化后可能撞车（name 与某个别名同形）
            return
        seen.add(key)
        rows.append((raw, key, sid, source))

    add(rec.get("name") or "", "name")
    add(rec.get("name_cn") or "", "name_cn")
    for a in infobox_aliases(rec.get("infobox") or ""):
        add(a, "infobox_alias")
    return rows


def build_row(rec: dict, stats: Counter) -> dict[str, Any]:
    tags = clean_tags(rec.get("tags"), stats)
    details = rec.get("score_details") or {}
    score_count = sum(int(v) for v in details.values()) if details else None

    date_raw = rec.get("date") or ""
    air_date = date_raw if len(date_raw) == 10 else None   # dump 实测：要么完整要么空
    if air_date is None:
        stats["no_toplevel_date"] += 1

    rank = rec.get("rank") or None                          # Bangumi 用 0 表示未排名
    fav = rec.get("favorite") or {}
    aliases = build_aliases(rec)

    # BM25 三档权重：A=标题与别名，B=题材 tag，C=简介
    # 标题命中显然该排在简介命中前面，这是 tsvector 白送的能力，不用白不用
    tsv_title = tokenize(" ".join(a[0] for a in aliases))
    tsv_tags = tokenize(" ".join(t["name"] for t in tags))
    tsv_sum = tokenize(rec.get("summary") or "")

    return {
        "subject_id": rec["id"],
        "name": rec.get("name") or "",
        "name_cn": (rec.get("name_cn") or "").strip() or None,
        "summary": (rec.get("summary") or "").strip() or None,
        "air_date": air_date,
        "air_year": candidates.parse_year(rec),
        "form": candidates.form_of(rec),
        "platform": rec.get("platform"),
        "nsfw": bool(rec.get("nsfw")),
        "score": rec.get("score") or None,
        "score_count": score_count,
        "score_details": Jsonb(details) if details else None,
        "rank": rank,
        "fav_done": int(fav.get("done") or 0),
        "favorite": Jsonb(fav) if fav else None,
        "tags": Jsonb(tags),
        "meta_tags": list(rec.get("meta_tags") or []),
        "tsv_title": tsv_title,
        "tsv_tags": tsv_tags,
        "tsv_sum": tsv_sum,
        "_aliases": aliases,
    }


# ------------------------------------------------------------------ 抽样

def pick_sample(n: int, seed: int) -> set[int]:
    """Top-N/热度 + 分层随机，用于 50 部试灌。

    不取 dump 前 N 条：前排全是冷门条目，看不出字段理解有没有错。
    也不做纯随机：真正会暴露 bug 的是少数派 ——
      · 顶层 date 为空、靠 infobox 补年份的（全库仅 213 部，纯随机基本抽不到）
      · nsfw 条目（780 部）
    所以热度前 30 保证「名字/评分/tag 一眼能看出对不对」，
    剩下 20 个名额分层抽，保证两条易错路径都被走到。
    """
    pool: list[tuple[int, int, bool, bool]] = []   # (id, done, 无顶层date, nsfw)
    for rec in tqdm(candidates.iter_candidates(), desc="扫描候选集", unit="部"):
        pool.append((
            rec["id"],
            int((rec.get("favorite") or {}).get("done") or 0),
            len(rec.get("date") or "") != 10,
            bool(rec.get("nsfw")),
        ))
    print(f"候选集共 {len(pool)} 部")

    rng = random.Random(seed)
    n_top = min(30, n)
    chosen = {r[0] for r in sorted(pool, key=lambda r: -r[1])[:n_top]}

    def draw(pred, k: int) -> None:
        avail = [r[0] for r in pool if r[0] not in chosen and pred(r)]
        for sid in rng.sample(avail, min(k, len(avail))):
            chosen.add(sid)

    remain = n - len(chosen)
    if remain > 0:
        draw(lambda r: r[2], max(1, remain // 4))          # 无顶层 date
        draw(lambda r: r[3], max(1, remain // 4))          # nsfw
        draw(lambda r: True, n - len(chosen))              # 其余随机补满
    return chosen


# ------------------------------------------------------------------ 写库

def flush(conn: psycopg.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    sids = [r["subject_id"] for r in rows]
    alias_rows = [a for r in rows for a in r["_aliases"]]
    payload = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    with conn.cursor() as cur:
        cur.executemany(UPSERT_PROFILE, payload)          # 先 profile，alias 有外键
        cur.execute(DELETE_ALIAS, (sids, list(DUMP_ALIAS_SOURCES)))
        cur.executemany(INSERT_ALIAS, alias_rows)
    conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="只灌 N 部（0 = 全量）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true", help="只跑解析不写库")
    args = ap.parse_args()

    print(f"分词词典指纹 dict_fingerprint = {dict_fingerprint()}")
    print(f"题材 tag 词表 {len(keep_tags())} 个\n")

    wanted = pick_sample(args.sample, args.seed) if args.sample else None

    stats: Counter = Counter()
    conn = None if args.dry_run else db.connect()
    buf: list[dict] = []
    n_done = n_alias = 0
    try:
        it = candidates.iter_candidates()
        for rec in tqdm(it, desc="灌库", unit="部"):
            if wanted is not None and rec["id"] not in wanted:
                continue
            row = build_row(rec, stats)
            n_alias += len(row["_aliases"])
            buf.append(row)
            n_done += 1
            if len(buf) >= BATCH:
                if conn:
                    flush(conn, buf)
                buf.clear()
            if wanted is not None and n_done >= len(wanted):
                break
        if conn:
            flush(conn, buf)
    finally:
        if conn:
            conn.close()

    print(f"\n写入 {n_done} 部，alias {n_alias} 行"
          f"（平均 {n_alias / max(n_done, 1):.1f} 个/部）")
    print("tag 分流统计：")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]):
        print(f"    {k:22} {v:>7}")


if __name__ == "__main__":
    main()

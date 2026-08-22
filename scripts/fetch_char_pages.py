"""阶段 06：抓萌娘百科角色页。

跑法：
    uv run --group etl python scripts/fetch_char_pages.py --top 500 --dry-run  # 先看账
    uv run --group etl python scripts/fetch_char_pages.py --top 500            # 真抓

前置：scripts/extract_char_links.py（产出 char_page_links.json）

⚠️ **合规与速率**（与 fetch_moegirl 同一套，直接复用它的 UA 和 delay）：
   · 7 秒/请求，**不要调低**。对方是公益站点，这里省下的时间没有价值。
   · UA 诚实：项目名 + 用途 + 联系方式，不伪装浏览器。
   · robots 的 ai-train=no ⇒ 这份语料**永不可用于训练/微调**（embedding 编码不算训练）。
   · CC BY-NC-SA：公开展示正文必须署名。

⚠️ **必须先 prop=info 再抓，这是省 7 小时的一步。** 候选角色链接里
   **47.2% 是红链**（实测随机抽样 1,200 个）—— 直接抓的话，前 500 部的
   8,157 个候选里约 3,800 个会 404，每个白等 7 秒 = **7.4 小时纯浪费**；
   而 prop=info 一次问 50 个，补齐 6,000 个未知只要约 14 分钟。

⚠️ **两阶段都幂等、可断点续跑** —— 首次跑 prop=info 时撞上 DNS 失败整个作废过
   （httpx.ConnectError: getaddrinfo failed）。存在性结果落 char_page_sizes.json，
   已抓的页面靠文件存在跳过，中途断了直接重跑即可。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from _keepawake import KeepAwake
from fetch_moegirl import DEFAULT_DELAY, REST, TITLES_PER_BATCH, UA, make_bar, resolve_titles

ROOT = Path(__file__).resolve().parent.parent
LINKS = ROOT / "data" / "interim" / "char_page_links.json"
SIZES = ROOT / "data" / "interim" / "char_page_sizes.json"
MOE_MANIFEST = ROOT / "data" / "raw" / "moegirl" / "manifest.jsonl"
TITLES_MAP = ROOT / "data" / "interim" / "moegirl_titles.json"
OUT_DIR = ROOT / "data" / "raw" / "moegirl_char"
OUT_MANIFEST = OUT_DIR / "manifest.jsonl"


def pick_targets(top_n: int, exclude_pageids: set[int] | frozenset = frozenset(),
                 since_year: int | None = None,
                 root_year: dict[int, int] | None = None):
    """选热度前 top_n 部作品的角色标题，并带上它们的 series_root。

    ⚠️ 一个角色可能同时属于多部作品（《Fate》系列尤其明显），所以作用域是
       **多对多** —— 这里就把 roots 收全，灌库时 plot_chunk_scope 直接用。
    📌 实测 136 个页面跨多部作品，其中 **133 个是同一 character_id**
       （韦伯出现在 Fate/Zero 和 FGO，两者 series_root 不同，映射到两个
       作用域是对的）；只有 3 个是不同 character_id 共用一页
       （JOJO / 韦伯·维尔维特 / 菲伦），占全部 1,493 页的 0.2%。
       ⇒ 不为它加消歧逻辑。真正的防线在 04b：提链接时就做了作用域交集判断，
         所以「アリス」这类泛名不会被挂到没关系的作品上。

    🚨 **exclude_pageids 挡的是「角色与作品同名」**：alias 里有叫「哆啦A梦」的
       角色，反查就把**作品页**当角色页抓回来。实测 5 个：犬夜叉 · 哆啦A梦 ·
       Free! · 幽灵公主 · 逆A高达。灌库时它会让 moegirl_page.kind 被改写、
       作品页原有 chunk 被覆盖，**两处损坏都不报错**（build_plot_chunks
       那边也挡了一道，这里挡是为了不白抓）。
    """
    links = json.loads(LINKS.read_text(encoding="utf-8"))
    heat = {}
    for line in MOE_MANIFEST.read_text(encoding="utf-8").splitlines():
        if line.strip():
            m = json.loads(line)
            heat[str(m["pageid"])] = m.get("heat") or 0
    tmap = json.loads(TITLES_MAP.read_text(encoding="utf-8"))
    page_roots: dict[str, set[int]] = defaultdict(set)
    for root, v in tmap.items():
        page_roots[str(v["pageid"])].add(int(root))

    ranked = sorted(links.items(), key=lambda kv: -heat.get(kv[0], 0))
    chosen = dict(ranked[:top_n])
    # ⚠️ **时间维度是「或」不是「与」**：新番热度天然低（候选集 done>=50 的
    #    口径决定的，见「季度更新」①），按热度排根本进不了前 N —— 而
    #    「最近几年的番」恰恰是用户最可能问的。两个口径取并集。
    if since_year and root_year:
        for pid, v in ranked:
            if pid in chosen:
                continue
            if any(root_year.get(r, 0) >= since_year
                   for r in page_roots.get(pid, ())):
                chosen[pid] = v
    targets: dict[str, set[int]] = defaultdict(set)
    for pid, v in chosen.items():
        for t in v["chars"]:
            targets[t] |= page_roots.get(pid, set())
    if exclude_pageids:
        sizes = json.loads(SIZES.read_text(encoding="utf-8")) if SIZES.exists() else {}
        dropped = [t for t in targets
                   if sizes.get(t) and sizes[t]["pageid"] in exclude_pageids]
        for t in dropped:
            del targets[t]
        if dropped:
            print(f"⚠️ 排除 {len(dropped)} 个「角色与作品同名」的页面："
                  f"{'、'.join(dropped[:5])}")
    return targets, [v["title"] for v in chosen.values()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=500, help="取热度前 N 部作品的角色页")
    ap.add_argument("--since-year", type=int, default=None,
                    help="额外纳入 air_year >= 该年份的作品（与 --top 取并集）")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="请求间隔秒数。⚠️ 7 秒是铁律，不要调低")
    ap.add_argument("--dry-run", action="store_true", help="只算账不发请求")
    args = ap.parse_args()

    # ⚠️ 连一次库只为拿「哪些 pageid 已经是作品页」。本脚本其余部分不碰库。
    from src import db
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pageid FROM moegirl_page WHERE kind = 'series'")
        series_pages = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT coalesce(series_root, subject_id), max(air_year) "
                    "FROM anime_profile WHERE air_year IS NOT NULL GROUP BY 1")
        root_year = dict(cur.fetchall())
    targets, works = pick_targets(args.top, series_pages,
                                  args.since_year, root_year)
    sizes = json.loads(SIZES.read_text(encoding="utf-8")) if SIZES.exists() else {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    unknown = [t for t in targets if t not in sizes]
    known_alive = [t for t in targets if sizes.get(t)]
    have = {f.name.split(".")[0] for f in OUT_DIR.glob("*.html.gz")}
    scope_desc = f"热度前 {args.top} 部"
    if args.since_year:
        scope_desc += f" + {args.since_year} 年以来的作品"
    print(f"{scope_desc} —— 共 {len(works)} 部（{works[0]} … {works[-1]}）")
    print(f"候选角色标题 {len(targets):,} 个")
    print(f"  已知存在 {len(known_alive):,} · 已知红链 "
          f"{sum(1 for t in targets if t in sizes and not sizes[t]):,} · "
          f"未查存在性 {len(unknown):,}")
    n_batch = -(-len(unknown) // TITLES_PER_BATCH)
    print(f"\n[1/2] prop=info 补齐 {len(unknown):,} 个 → {n_batch:,} 次请求 "
          f"≈ {n_batch * args.delay / 60:.0f} 分钟")
    est_alive = len(known_alive) + int(len(unknown) * 0.528)
    est_fetch = est_alive - len(have & {str(sizes[t]["pageid"]) for t in known_alive})
    print(f"[2/2] 抓取约 {est_fetch:,} 页 ≈ {est_fetch * args.delay / 3600:.1f} 小时"
          f"（已抓过 {len(have):,} 页会跳过）")
    if args.dry_run:
        return 0

    # 🚨 **防睡眠**：首次跑到 3h19m（1,502/4,955）被外部终止，日志零异常、
    #    fail 0 —— 系统休眠挂起了进程。translate_corpus 早就为它 8 小时的
    #    任务加过这个，这里当初漏了。见 scripts/_keepawake.py。
    with KeepAwake(), httpx.Client(headers={"User-Agent": UA}, timeout=60,
                                   follow_redirects=True) as c:
        if unknown:
            # ⚠️ 每 CHUNK 个标题存一次盘。调小是为了压低**最坏损失** ——
            #    重试用尽后整批会作废，实测 503 那次丢了 900 个已解析的结果。
            CHUNK = 500
            for i in range(0, len(unknown), CHUNK):
                part = unknown[i:i + CHUNK]
                found = resolve_titles(c, part, args.delay)
                for t in part:
                    sizes[t] = found.get(t)          # None = 红链，也要记，否则会反复重查
                SIZES.write_text(json.dumps(sizes, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
                print(f"  存在性已存盘 {i + len(part):,} / {len(unknown):,}", flush=True)

        todo = [t for t in targets if sizes.get(t)
                and str(sizes[t]["pageid"]) not in have]
        print(f"\n开始抓取 {len(todo):,} 页 · {args.delay:.0f}s/页 "
              f"≈ {len(todo) * args.delay / 3600:.1f} 小时", flush=True)
        bar = make_bar(len(todo), "抓角色页", "页")
        ok = fail = 0
        with OUT_MANIFEST.open("a", encoding="utf-8") as mf:
            for t in todo:
                v = sizes[t]
                try:
                    r = c.get(f"{REST}/{quote(v['title'], safe='')}/html")
                    r.raise_for_status()
                    (OUT_DIR / f"{v['pageid']}.html.gz").write_bytes(
                        gzip.compress(r.text.encode("utf-8")))
                    mf.write(json.dumps({
                        "pageid": v["pageid"], "title": v["title"],
                        "lastrevid": v.get("lastrevid"), "wikitext_len": v.get("length"),
                        "html_len": len(r.text), "asked": t,
                        "series_roots": sorted(targets[t]),
                        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }, ensure_ascii=False) + "\n")
                    mf.flush()
                    ok += 1
                except (httpx.HTTPError, OSError) as e:
                    # ⚠️ 单页失败不该中断 9 小时的任务；但只吞网络/IO 错，
                    #    别的异常是真 bug，让它炸出来。KeyboardInterrupt 同样放行。
                    fail += 1
                    bar.write(f"  ✗ {t}: {type(e).__name__}")
                bar.update(1)
                bar.set_postfix_str(f"ok {ok:,} fail {fail}", refresh=False)
                time.sleep(args.delay)
        bar.close()
    print(f"\n完成：成功 {ok:,} · 失败 {fail}")
    print(f"→ {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

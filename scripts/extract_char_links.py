"""阶段 04b：从已抓的萌娘作品页里提角色页链接，为阶段 06 算准体积。

跑法：
    uv run --group etl python scripts/extract_char_links.py            # 本地提链接
    uv run --group etl python scripts/extract_char_links.py --sizes    # 再联网求 length

⚠️ **不按章节标题定位角色** —— 各页结构差异极大，实测：
     《药屋少女的呢喃》有「登场人物」节，但里面角色与声优混排；
     《进击的巨人》**根本没有**「登场人物」节，角色藏在 CAST 里（175 内链），
       而「题外话」一节有 2,059 个导航噪声。
   ⇒ 改成**全页提内链 + alias 表反查**：链接名在本页作用域内能匹配到
     character 行才算角色页。这正是阶段 03 灌 196,669 行角色 alias 时
     特意留 parent_subject_id 的用途（「角色消歧必须锚定在已确认的
     subject 范围内」）。

⚠️ 这样得到的是**下界**：萌娘有而 Bangumi dump 没有的角色会漏掉。
   但阶段 06 的目的正是补「dump 有角色、没有中文简介」的那批，以 alias
   为准与目标一致；而估体积时下界比虚高的数字安全。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import html as lxml_html
from tqdm import tqdm

# ⚠️ 复用 fetch_moegirl 的 UA / 7 秒节流 / resolve_titles，**不另写一份** ——
#    与 build_plot_chunks 复用 build_embeddings 编排同一条理由：两份实现
#    迟早用不同的速率打同一个站，而漂移不报错。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_moegirl import DEFAULT_DELAY, TITLES_PER_BATCH, UA, resolve_titles

from src import db
from src.textproc import norm_name

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "moegirl"
MANIFEST = RAW_DIR / "manifest.jsonl"
TITLES = ROOT / "data" / "interim" / "moegirl_titles.json"
OUT = ROOT / "data" / "interim" / "char_page_links.json"
SIZES = ROOT / "data" / "interim" / "char_page_sizes.json"

# 萌娘的消歧后缀：「猫猫(药屋少女的呢喃)」。半角全角都有。
DISAMBIG = re.compile(r"[（(][^）)]*[）)]$")
# 非条目链接：Special:/Category:/File: 这类，以及模板命名空间
NON_ARTICLE = re.compile(r"^(Special|Category|File|Template|Help|Talk|"
                         r"分类|文件|模板|帮助|讨论|User|用户):", re.IGNORECASE)


def load_alias(conn) -> dict[str, set[int]]:
    """norm_name → {parent_subject_id}，只取角色行。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT norm_name, parent_subject_id FROM alias
             WHERE entity_type = 'character' AND parent_subject_id IS NOT NULL
        """)
        out: dict[str, set[int]] = defaultdict(set)
        for nm, sid in cur.fetchall():
            out[nm].add(sid)
    return out


def load_series_root(conn) -> dict[int, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT subject_id, coalesce(series_root, subject_id) "
                    "FROM anime_profile")
        return dict(cur.fetchall())


def page_links(html: str) -> list[str]:
    """全页内链的条目标题（已去重、去掉非条目命名空间）。"""
    doc = lxml_html.fromstring(html)
    seen, out = set(), []
    for a in doc.xpath('.//a[@rel="mw:WikiLink"]'):
        t = (a.get("title") or "").strip()
        if not t or t in seen or NON_ARTICLE.match(t):
            continue
        seen.add(t)
        out.append(t)
    return out


def measure_sizes(delay: float, sample: int = 0, seed: int = 0) -> int:
    """对候选角色标题批量查 prop=info，拿存在性 + wikitext 字节数。

    ⚠️ **红链必须先挡掉。** 萌娘作品页里的角色链接有相当一部分指向尚未建立
       的条目（红链），抓它们纯属浪费那 7 秒。存在性只有 API 能回答。
    ⚠️ length 是 **wikitext 字节数**，不是 chunk 数。作品页实测换算率
       0.469 chunk/KB，但**不能直接套到角色页**：作品页的长度大量来自表格
       和列表（我们本来就丢），而角色页的长度来自「个人经历」这类散文，
       是要留的 —— 换算率大概率接近 0.889 那一档。
       ⇒ 拿到 length 之后还要抽 30 页真抓一次校准，见 CLAUDE.md 六阶段计划。
    """
    import httpx
    data = json.loads(OUT.read_text(encoding="utf-8"))
    titles = sorted({t for v in data.values() for t in v["chars"]})
    if sample and sample < len(titles):
        # 🚨 **必须随机抽，不能取 sorted 的前 N 个。** 首次就是这么错的：
        #    前 1,000 个全是数字/符号开头的代号角色（'02' '119(炎炎消防队)'
        #    '13号(我的英雄学院)'），红链率 44.1% —— 而那是这一类的特征，
        #    不是全体的。⚠️ 这类偏差看起来一切正常，只有把样本打印出来才看得见。
        import random
        titles = sorted(random.Random(seed).sample(titles, sample))
        print(f"随机抽样 {sample:,} / {len(data):,} 页的候选（seed={seed}）")
    n_batch = -(-len(titles) // TITLES_PER_BATCH)
    print(f"候选角色标题 {len(titles):,} 个 → {n_batch:,} 次请求 "
          f"× {delay:.0f}s ≈ {n_batch * delay / 60:.0f} 分钟")
    # ⚠️ **断点续跑是必需的，不是锦上添花。** 首次全量跑到 150/16,569 时
    #    撞上 DNS 解析失败整个作废（httpx.ConnectError: getaddrinfo failed）。
    #    44 分钟的任务不该被一次网络抖动清零 —— fetch_moegirl 本身就是这个规格。
    # ⚠️ 查过但不存在的标题存 None，**不能只存命中的** —— 否则重跑时
    #    分不清「没查过」和「查过是红链」，红链会被反复重查。
    store: dict = {}
    if SIZES.exists():
        store = json.loads(SIZES.read_text(encoding="utf-8"))
        print(f"续跑：已有 {len(store):,} 个标题的结果，跳过")
    todo = [t for t in titles if t not in store]
    if not todo:
        print("全部已查过，直接汇总")

    CHUNK = 1000                      # 每 1,000 个标题存一次盘
    with httpx.Client(headers={"User-Agent": UA}, timeout=30,
                      follow_redirects=True) as c:
        for i in range(0, len(todo), CHUNK):
            part = todo[i:i + CHUNK]
            found = resolve_titles(c, part, delay)
            for t in part:
                store[t] = found.get(t)
            SIZES.write_text(json.dumps(store, ensure_ascii=False, indent=1),
                             encoding="utf-8")
            print(f"  已存盘 {len(store):,} / {len(titles):,}", flush=True)

    # ⚠️ 统计只看**本次 titles 子集** —— store 里可能混着此前那批有偏样本，
    #    一起算会把偏差带进结论。
    sub = {t: store[t] for t in titles if t in store}
    found = {k: v for k, v in sub.items() if v}
    lens = sorted((v.get("length") or 0) for v in found.values())
    n = len(lens)
    total_kb = sum(lens) / 1024
    dead = len(sub) - n            # 本次样本里有多少是红链
    print()
    print(f"存在 {n:,} 个 · 红链/不存在 {dead:,} 个 ({100 * dead / max(len(sub), 1):.1f}%)")
    if n:
        def pct(p): return lens[min(int(n * p), n - 1)]
        print(f"wikitext 字节 中位 {pct(0.5):,} · p90 {pct(0.9):,} · max {lens[-1]:,}")
        print(f"总计 {total_kb:,.0f} KB")
        for rate, note in ((0.469, "作品页实测（偏低，角色页多半不适用）"),
                           (0.889, "短页面那一档（角色页更可能接近这个）")):
            print(f"  × {rate} chunk/KB → {total_kb * rate:,.0f} chunk   {note}")
    print()
    if sample:
        alive = n / max(len(sub), 1)
        full = int(len({t for v in data.values() for t in v["chars"]}) * alive)
        print(f"外推全量：存活 {full:,} 页 × 7s ≈ {full * 7 / 3600:.1f} 小时")
    else:
        print(f"抓取耗时估计：{n:,} 页 × 7s ≈ {n * 7 / 3600:.1f} 小时")
    print(f"→ {SIZES}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, help="只处理前 N 页")
    ap.add_argument("--sizes", action="store_true",
                    help="第二阶段：联网查 prop=info 求存在性与 length")
    ap.add_argument("--sample", type=int, default=0,
                    help="只随机抽查 N 个标题（估比例用，比全量快一个数量级）")
    ap.add_argument("--seed", type=int, default=0, help="抽样种子，保证可复现")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="请求间隔秒数。⚠️ 7 秒是铁律，不要调低")
    args = ap.parse_args()

    if args.sizes:
        return measure_sizes(args.delay, args.sample, args.seed)

    with db.connect() as conn:
        alias = load_alias(conn)
        sroot = load_series_root(conn)
    print(f"alias 角色名 {len(alias):,} 个 · anime_profile {len(sroot):,} 部")

    tmap = json.loads(TITLES.read_text(encoding="utf-8"))
    page_roots: dict[int, set[int]] = defaultdict(set)
    for root, v in tmap.items():
        page_roots[v["pageid"]].add(int(root))

    rows = [json.loads(x) for x in MANIFEST.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = list({r["pageid"]: r for r in rows}.values())
    if args.limit:
        rows = rows[:args.limit]

    result, per_page = {}, []
    n_links = n_hit = 0
    for r in tqdm(rows, desc="提链接", unit="页"):
        pid = r["pageid"]
        f = RAW_DIR / f"{pid}.html.gz"
        if not f.exists():
            continue
        roots = page_roots.get(pid, set())
        hits = []
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            links = page_links(fh.read())
        for t in links:
            n_links += 1
            # 原名与去消歧后缀名都试一遍
            for cand in {t, DISAMBIG.sub("", t).strip()}:
                key = norm_name(cand)
                owners = alias.get(key)
                if owners and {sroot.get(s) for s in owners} & roots:
                    hits.append(t)
                    break
        hits = sorted(set(hits))
        n_hit += len(hits)
        result[pid] = {"title": r["title"], "chars": hits}
        per_page.append(len(hits))

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    uniq = {t for v in result.values() for t in v["chars"]}
    per_page.sort()
    n = len(per_page)
    def pct(p): return per_page[min(int(n * p), n - 1)] if n else 0
    print(f"\n页面 {n:,} 个 · 全页内链 {n_links:,} 条 · 判为角色页 {n_hit:,} 条")
    print(f"去重后角色页标题 {len(uniq):,} 个")
    print(f"每页角色数 中位 {pct(0.5)} · p90 {pct(0.9)} · p99 {pct(0.99)} · max {per_page[-1] if n else 0}")
    print(f"零角色的页面 {sum(1 for x in per_page if x == 0):,} 个")
    top = Counter({v["title"]: len(v["chars"]) for v in result.values()})
    print("角色最多的页：", top.most_common(6))
    print(f"\n→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

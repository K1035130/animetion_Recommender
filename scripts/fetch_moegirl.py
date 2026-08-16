"""抓萌娘百科条目正文 —— 第 4 周批次 2 的语料来源。

**只抓不解析。** 落原始 HTML 到 data/raw/moegirl/，切 chunk / 打剧透标记 / 编码
都是后续独立阶段。这是 docs/phase2-overseas-data.md 里 `ingest_raw` 的同一套思路：
先把原始响应存下来，**切分策略改了不用重抓**（而重抓要几小时且给对方添麻烦）。

--------------------------------------------------------------------------
⚠️ 合规（2026-08-15 查 robots.txt 实录，动手前必读）
--------------------------------------------------------------------------
    User-agent: *
    Content-Signal: search=yes,ai-train=no,use=reference
    Allow: /

另有一份点名封禁的清单：GPTBot / ClaudeBot / CCBot / Google-Extended /
Bytespider / Amazonbot / Applebot-Extended / meta-externalagent → Disallow: /

· 我们走 `User-agent: *` 那条，是 Allow。
· **ai-train=no ⇒ 这份语料永远不能拿去训练或微调模型。** embedding 是编码不是
  训练，检索也不是训练 —— 本项目的用法不触碰这条，但它是硬约束，别越界。
· ai-input（RAG / grounding）**未列出**，按 robots.txt 自己给的规则 (c)：
  「既未授权也未禁止」。剧情问答落在这一格。
· use=reference ⇒ 以「引用」方式消费，与流程 C 的形态一致。
· 正文许可 CC BY-NC-SA 3.0（署名-非商业-相同方式共享）。作品集项目符合非商业；
  ⚠️ 将来若要公开展示 chunk 正文，**必须署名并注明许可**。

⚠️ **UA 必须诚实。** 写明项目名和联系方式，不伪装浏览器，更不能冒用上面那批
   被封禁的 bot 名字 —— 那是在明确拒绝之后绕过拒绝。

--------------------------------------------------------------------------
⚠️ api.php 的取内容动作对匿名用户全部禁用（实测）
--------------------------------------------------------------------------
    action=parse                 → action-notallowed
    action=query&prop=revisions  → action-notallowed（Unauthorized API call）
    action=query&list=search     → action-notallowed
    index.php?action=raw         → 返回渲染页而非 wikitext，且 robots 禁 action=

**可用的只有两条**，本脚本就建立在它们之上：

    api.php?action=query&prop=info&titles=A|B|C&redirects=1   ← 一次 50 个标题
        给出：存在性(missing) · 重定向解析 · pageid · lastrevid · length
    rest.php/v1/page/{title}/html                             ← 正文
        heimu 剧透标记就在里面：<span title="你知道的太多了" class="heimu">…</span>

💡 prop=info 顺带给了 lastrevid，**所以不需要每页再请求一次元数据** —— 省掉一半
   请求。length（wikitext 字节数）还能在抓之前估算 chunk 数量。

--------------------------------------------------------------------------
两个阶段，各自幂等、可断点续跑
--------------------------------------------------------------------------
  resolve  读库取候选标题 → 批量解析 → data/interim/moegirl_titles.json
  fetch    按解析结果逐页抓 → data/raw/moegirl/{pageid}.html.gz + manifest.jsonl

⚠️ **三段式：读库 → 长耗时网络 → 写文件，中间不持有 Neon 连接。**
   Neon 是 serverless，空闲连接会被回收 —— build_embeddings.py 已经踩过一次
   （API 阶段 11 分钟全部成功，写库时 SSL connection has been closed）。

用法：
    uv run python scripts/fetch_moegirl.py --limit 50      # 先跑小样本（原则 5）
    uv run python scripts/fetch_moegirl.py                 # 全量 2000 个系列
    uv run python scripts/fetch_moegirl.py --resolve-only  # 只解析标题不抓正文
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db

HOST = "zh.moegirl.org.cn"
API = f"https://{HOST}/api.php"
REST = f"https://{HOST}/rest.php/v1/page"

# ⚠️ 诚实的 UA：项目名 + 用途 + 联系方式。见文件顶部的合规说明。
UA = ("animetion-recommender/0.1 "
      "(+https://github.com/K1035130/animetion_Recommender; "
      "anime recommender, portfolio project; contact kevin1035130@outlook.com)")

# 礼貌速率（秒）。设计定的是 1 req/s，实测服务端 cache MISS 自身就要 ~2.9 s。
# ⚠️ **2026-08-15 Kevin 定：进一步放慢，全量跑 4 小时左右即可。**
#    抓取不在关键路径上（跑完一次就够，之后都读本地文件），
#    而对方是公益站点 —— 这里省下来的时间没有价值，少给人添麻烦有价值。
# 单页总耗时 ≈ DELAY + 2.5s（服务端），1,466 页 × (7+2.5) ≈ 3.9 小时。
DEFAULT_DELAY = 7.0
TITLES_PER_BATCH = 50          # prop=info 的上限
DEFAULT_SERIES = 2000          # 批次 2 的目标：~2000 个系列，覆盖约 4,040 部作品

# ⚠️ 进度条在**非 TTY**（重定向到日志文件）时必须压低刷新频率。
#    tqdm 在非 TTY 下每次刷新都追加一段输出，而 set_postfix_str 会触发刷新 ——
#    实测不压的话几百页就把日志刷成几千行，`tail` 完全没法看。
#    ⇒ TTY 下 0.5 秒刷一次（看着流畅），非 TTY 下 30 秒一次（日志够用）。
TTY = sys.stderr.isatty()
BAR_INTERVAL = 0.5 if TTY else 30.0


def make_bar(total: int, desc: str, unit: str) -> tqdm:
    return tqdm(total=total, desc=desc, unit=unit, ascii=True, ncols=78,
                mininterval=BAR_INTERVAL)


RAW_DIR = Path("data/raw/moegirl")
MANIFEST = RAW_DIR / "manifest.jsonl"
TITLE_MAP = Path("data/interim/moegirl_titles.json")


# ============================================================
# 阶段一：从库里取候选标题
# ============================================================
def load_candidates(n_series: int) -> list[dict]:
    """每个系列一组候选标题，按优先级排好。

    ⚠️ **不能只用系列根的名字。** series_root 取的是系列里最早播出的那部，
       而柯南的根是《名侦探柯南 计时引爆摩天楼》（剧场版比 TV 早），
       拿它去查萌娘百科会查不到 —— 正确的条目名是《名侦探柯南》。
       所以候选要覆盖系列内**所有成员**的中日文名 + alias 表里的别名。

    优先级：根的中文名 → 根的原名 → 其余成员的中文名 → 成员原名 → 别名。
    """
    sql = """
    WITH s AS (
        SELECT coalesce(series_root, subject_id) AS root,
               sum(fav_done) AS heat
          FROM anime_profile
         WHERE NOT nsfw
         GROUP BY 1
         ORDER BY heat DESC
         LIMIT %s
    ), members AS (
        SELECT s.root, s.heat, p.subject_id, p.name, p.name_cn,
               (p.subject_id = s.root) AS is_root
          FROM s
          JOIN anime_profile p
            ON coalesce(p.series_root, p.subject_id) = s.root
    )
    SELECT m.root, m.heat,
           -- 优先级越小越靠前
           array_agg(DISTINCT t.title ORDER BY t.title) FILTER (WHERE t.title <> '')
      FROM members m
      CROSS JOIN LATERAL (VALUES (m.name_cn), (m.name)) AS t(title)
     WHERE coalesce(t.title, '') <> ''
     GROUP BY m.root, m.heat
     ORDER BY m.heat DESC
    """
    # ⚠️ 上面的 array_agg 丢掉了优先级信息，所以优先级在 Python 侧重排（见下）。
    #    SQL 里排会让语句复杂一倍，而这里只有几千行，Python 排是零成本。
    out: list[dict] = []
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (n_series,))
        rows = cur.fetchall()

        # 取每个 root 自己的名字，用来把它顶到候选首位
        cur.execute("""
            SELECT subject_id, name_cn, name FROM anime_profile
             WHERE subject_id = ANY(%s)
        """, ([r[0] for r in rows],))
        rootname = {sid: (cn, jp) for sid, cn, jp in cur.fetchall()}

    for root, heat, titles in rows:
        titles = [t.strip() for t in (titles or []) if t and t.strip()]
        cn, jp = rootname.get(root, (None, None))
        head = [t for t in (cn, jp) if t]                 # 根的名字优先
        ordered = head + [t for t in titles if t not in head]
        # 去重且保序
        seen, cand = set(), []
        for t in ordered:
            if t not in seen:
                seen.add(t)
                cand.append(t)
        out.append({"root": root, "heat": heat, "candidates": cand})
    return out


# ============================================================
# 阶段二：批量解析标题
# ============================================================
def resolve_titles(client: httpx.Client, titles: list[str],
                   delay: float) -> dict[str, dict]:
    """批量查存在性 + 重定向，一次 50 个。

    返回 {查询用的标题: {title, pageid, lastrevid, length}}，不存在的不在结果里。
    ⚠️ redirects=1 让 API 自己跟随重定向（命运石之门 → 命运石之门系列），
       返回的 redirects 数组记录了 from→to，要用它把结果映射回原查询词。
    """
    found: dict[str, dict] = {}
    # ⚠️ ascii=True：Windows 控制台对 unicode 方块字符不总是可靠，用 ASCII 稳妥。
    #    非 TTY（重定向到日志文件）时 tqdm 照样写，只是没有回车动画 —— 用
    #    mininterval 压低写入频率，避免把日志刷爆。
    bar = make_bar(len(titles), "解析标题", "标题")
    for i in range(0, len(titles), TITLES_PER_BATCH):
        batch = titles[i:i + TITLES_PER_BATCH]
        # ⚠️ 必须用 POST。GET 会 414 URI Too Long —— 中文标题 percent-encode 后
        #    每个汉字 9 字节，50 个标题轻松超过 URL 长度上限。
        #    MediaWiki 的 action=query 是读操作但同样接受 POST。
        r = client.post(API, data={
            "action": "query", "prop": "info", "redirects": "1",
            "titles": "|".join(batch), "format": "json", "formatversion": "2",
        })
        r.raise_for_status()
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"API 报错: {d['error']}")
        q = d.get("query", {})

        # normalized / redirects 把「查询用的写法」映射到「实际条目名」
        alias: dict[str, str] = {}
        for key in ("normalized", "redirects"):
            for m in q.get(key, []):
                alias[m["from"]] = m["to"]

        # ⚠️ 除了 missing 还要挡 invalid，而且必须直接检查 pageid 在不在。
        #    MediaWiki 的标题不允许 [ ] 等字符，而 Bangumi 的原名里很常见
        #    （`Fate/stay night [Unlimited Blade Works]`）—— 这类返回的是
        #    invalid:true，**没有 pageid 字段**，只判 missing 会 KeyError。
        by_title = {p["title"]: p for p in q.get("pages", [])
                    if "pageid" in p and not p.get("missing")}
        for asked in batch:
            final = asked
            for _ in range(3):                      # 重定向最多跟三跳
                final = alias.get(final, final)
            pg = by_title.get(final)
            if pg:
                found[asked] = {
                    "title": pg["title"], "pageid": pg["pageid"],
                    "lastrevid": pg.get("lastrevid"), "length": pg.get("length"),
                }
        bar.update(len(batch))
        bar.set_postfix_str(f"命中 {len(found):,}", refresh=False)
        time.sleep(delay)
    bar.close()
    return found


def do_resolve(n_series: int, delay: float) -> dict:
    print(f"[1/2] 读库取候选标题（前 {n_series:,} 个系列）…", flush=True)
    groups = load_candidates(n_series)                 # ⚠️ 读完就断开，见三段式
    all_titles = sorted({t for g in groups for t in g["candidates"]})
    print(f"      {len(groups):,} 个系列 · {len(all_titles):,} 个候选标题", flush=True)

    print(f"[2/2] 批量解析（{-(-len(all_titles) // TITLES_PER_BATCH)} 次请求）…", flush=True)
    with httpx.Client(headers={"User-Agent": UA}, timeout=30,
                      follow_redirects=True) as c:
        found = resolve_titles(c, all_titles, delay)

    # 每个系列取优先级最高的命中
    mapping, miss = {}, []
    for g in groups:
        for cand in g["candidates"]:
            if cand in found:
                mapping[str(g["root"])] = {**found[cand], "matched_from": cand,
                                           "heat": g["heat"]}
                break
        else:
            miss.append(g)

    TITLE_MAP.parent.mkdir(parents=True, exist_ok=True)
    TITLE_MAP.write_text(json.dumps(mapping, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    pages = {v["pageid"] for v in mapping.values()}
    print(f"\n解析完成：{len(mapping):,}/{len(groups):,} 个系列命中 "
          f"({len(mapping) / len(groups):.1%}) · 去重后 {len(pages):,} 个条目")
    print(f"→ {TITLE_MAP}")
    if miss:
        print(f"\n未命中 {len(miss):,} 个，热度最高的 10 个：")
        for g in miss[:10]:
            print(f"   {g['heat']:>8,}  {g['candidates'][0][:40]}")
    return mapping


# ============================================================
# 阶段三：抓正文
# ============================================================
def do_fetch(mapping: dict, limit: int | None, delay: float) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # 按 pageid 去重。⚠️ 实测碰撞很少：1,516 个系列只压成 1,494 个条目（22 处）。
    # 碰撞的**不是剧场版** —— 剧场版在萌娘百科基本各有独立条目
    # （名侦探柯南 零的执行人 / 绀青之拳 都是单独页）。真正会撞的是三类：
    #   ① 重制/不同改编同名：猎人(1999 vs 2011) · JOJO(OVA vs TV) · Kanon(东映 vs 京阿尼)
    #   ② Bangumi 拆成独立系列而萌娘合并：摇曳露营△ / 房间露营△
    #   ③ 译名不同的同一作品：堀与宫村 / 堀桑与宫村君
    # 即「Bangumi 按制作拆、萌娘按作品合」，所以去重必须按 pageid 而不是按标题。
    pages: dict[int, dict] = {}
    for v in sorted(mapping.values(), key=lambda x: -x["heat"]):
        pages.setdefault(v["pageid"], v)
    todo = [v for v in pages.values()
            if not (RAW_DIR / f"{v['pageid']}.html.gz").exists()]
    have = len(pages) - len(todo)            # ⚠️ 要在 --limit 截断之前算
    if limit:
        todo = todo[:limit]

    print(f"\n条目 {len(pages):,} 个 · 已有 {have:,} · 本次抓 {len(todo):,} "
          f"· 预计 {len(todo) * (delay + 2.5) / 60:.0f} 分钟", flush=True)
    if not todo:
        return

    ok = fail = 0
    bar = make_bar(len(todo), "抓取", "页")
    with (httpx.Client(headers={"User-Agent": UA}, timeout=60,
                       follow_redirects=True) as c,
          MANIFEST.open("a", encoding="utf-8") as mf):
        for v in todo:
            title, pid = v["title"], v["pageid"]
            try:
                # ⚠️ safe="" 让 / 也被编码 —— 条目名里带斜杠很常见
                # （「命运石之门系列/动画」），不编码会被当成路径分隔符打错端点
                r = c.get(f"{REST}/{quote(title, safe='')}/html")
                if r.status_code != 200:
                    # ⚠️ 用 bar.write 不用 print —— print 会把进度条冲散
                    bar.write(f"  x {r.status_code} {title}")
                    fail += 1
                    bar.update(1)
                    time.sleep(delay)
                    continue
                html = r.text
                (RAW_DIR / f"{pid}.html.gz").write_bytes(
                    gzip.compress(html.encode("utf-8")))
                mf.write(json.dumps({
                    "pageid": pid, "title": title, "lastrevid": v.get("lastrevid"),
                    "wikitext_len": v.get("length"), "html_len": len(html),
                    "heat": v["heat"], "matched_from": v.get("matched_from"),
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }, ensure_ascii=False) + "\n")
                mf.flush()          # ⚠️ 每条就刷，中断了也不丢已抓的记录
                ok += 1
            except Exception as e:                      # noqa: BLE001
                bar.write(f"  x {type(e).__name__} {title}: {e}")
                fail += 1
            bar.update(1)
            bar.set_postfix_str(f"ok {ok:,} 失败 {fail:,}", refresh=False)
            time.sleep(delay)
    bar.close()

    total = sum(f.stat().st_size for f in RAW_DIR.glob("*.html.gz"))
    print(f"\n完成：成功 {ok:,} · 失败 {fail:,}")
    print(f"落盘 {RAW_DIR} · {total / 1e6:.1f} MB（gzip）· 清单 {MANIFEST}")


def main() -> None:
    ap = argparse.ArgumentParser(description="抓萌娘百科条目正文（只抓不解析）")
    ap.add_argument("--series", type=int, default=DEFAULT_SERIES,
                    help=f"取热度前 N 个系列（默认 {DEFAULT_SERIES}）")
    ap.add_argument("--limit", type=int,
                    help="本次最多抓几页（先跑小样本，原则 5）")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help=f"每次请求间隔秒数（默认 {DEFAULT_DELAY}，别调低）")
    ap.add_argument("--resolve-only", action="store_true", help="只解析标题不抓正文")
    ap.add_argument("--reuse-titles", action="store_true",
                    help="复用已有的 moegirl_titles.json，跳过解析")
    a = ap.parse_args()

    if a.reuse_titles and TITLE_MAP.exists():
        mapping = json.loads(TITLE_MAP.read_text(encoding="utf-8"))
        print(f"复用 {TITLE_MAP}：{len(mapping):,} 个系列")
    else:
        mapping = do_resolve(a.series, a.delay)

    if not a.resolve_only:
        do_fetch(mapping, a.limit, a.delay)


if __name__ == "__main__":
    main()

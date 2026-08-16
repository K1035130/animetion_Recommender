"""补救标题解析漏掉的系列 —— 让**已有或易得**的语料能被找到。

⚠️ 这不是"再抓一批新内容"，是修「明明在抓取范围内、却因为标题写法对不上而
   整个系列没有语料」的那批。2026-08-16 实测的触发案例：

     银河特急 银河☆地铁   热度排名第 844（远在前 3,500 内），零语料
       Bangumi name_cn  银河特急 银河☆地铁
       萌娘实际标题      银河特急 银河地铁      ← 只差一个 ☆

问题的两类，解法是同一个（候选标题生成得不够）：

  ① 装饰符号不一致   ☆★✩・♪ 在两边写法不同
  ② 副标题/版本后缀   Bangumi 按制作拆，萌娘按作品合（E.3）
       剧场版 魔法少女小圆 [前篇] 起始的物语  → 萌娘用「魔法少女小圆」
       幸运星 OVA                        → 萌娘用「幸运星」
       刀剑神域 Extra Edition             → 萌娘用「刀剑神域」

⚠️ **两类的可信度不同，脚本分开报告、也分开应用：**
     A 类（符号/空格差异）  同一部作品的不同写法 → 安全，可直接建映射
     B 类（副标题剥离）     把衍生作映射到母条目 → **要人工过一眼**再应用

跑法：
    uv run --group etl python scripts/rescue_moegirl_titles.py            # 只报告
    uv run --group etl python scripts/rescue_moegirl_titles.py --apply-a  # 应用 A 类
    uv run --group etl python scripts/rescue_moegirl_titles.py --apply-a --apply-b

成本：prop=info 一次 50 个标题。1,347 个系列约 30 次请求 ≈ 3 分钟（7 秒/次）。
⚠️ **本脚本不抓正文。** 它只解析标题，产出两样东西：
     · 指向**已抓页面**的 → 直接建 plot_chunk_scope 映射，零抓取
     · 指向**新页面**的   → 写进 data/interim/moegirl_titles_rescue.json，
                            供之后用 fetch_moegirl.py --reuse-titles 抓
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fetch_moegirl import TITLE_MAP, UA, resolve_titles

from src import db

OUT = Path(__file__).resolve().parent.parent / "data" / "interim" / "moegirl_titles_rescue.json"

# 装饰性符号：出现在标题里、两边写法常不一致的那些。
# ⚠️ **不含 `・`（日文中点）** —— 它是日文标题的正规组成部分
#    （ソードアート・オンライン），去掉会造出根本不存在的写法。
SYMBOLS = re.compile(r"[☆★✩✧✦♪♫♬✿❀◆◇※♡❤∞＆〜～]")

# 副标题/版本后缀。⚠️ 顺序有意义：长的先匹配，否则「剧场总集篇」会被「剧场版」吃掉。
PREFIXES = ["剧场总集篇", "劇場総集編", "剧场版 ", "劇場版 ", "剧场版", "劇場版",
            "TV动画 ", "OVA "]
SUFFIXES = [" OVA", " OAD", " SP", " Extra Edition", " Special", " 剧场版",
            " 总集篇", " 特别篇", " 番外篇", " 完结篇", " 前篇", " 后篇",
            " 第一季", " 第二季", " 第三季", " 第四季",
            " 第1季", " 第2季", " 第3季",
            " 第一期", " 第二期", " 第三期", " 第四期"]
# 从这些分隔符处截断，只留前半
CUTS = ["：", ":", " - ", " — ", "〈", "【", "[", "（", "("]

# ⚠️ **截断守卫。** 2026-08-16 实测教训：
#    「Re：从零开始的异世界生活 雪之回忆」在 `：` 处截断得到「Re」，
#    而萌娘的「Re」条目**重定向到《哲学》** —— 一条静默的错误映射。
#    代价不对称：漏掉一部只是没语料（解析步骤的状态 ③ 能优雅处理），
#    而错配是拿另一部作品的剧情去回答，且看起来完全正常。⇒ 宁可漏。
#
# ⚠️ **判据是「截掉了多大比例」，不是「剩下几个字」。** 一度用过长度下限 4，
#    结果把「幸运星 OVA → 幸运星」这类正确映射一起杀了 —— 中文三字标题
#    （幸运星 / 航海王 / 咲日和）极常见。而「Re」的问题是它只占原标题的 10%。
# ⚠️ 比例守卫**只对截断生效**，前缀/后缀剥离不受影响（那两种本来就精确）。
CUT_MIN_RATIO = 0.3
CUT_MIN_LEN = 2

# 全角/半角标点互转。⚠️ 这才是「Re：」那个案例的正解：
#    我们的名字 Re：从零开始的异世界生活（全角）
#    萌娘的标题 Re:从零开始的异世界生活（半角）—— 而该页**早就抓下来了**。
#    截断是绕路且危险，归一化是直路。
WIDTH = str.maketrans("：！？（）；，～", ":!?();,~")
WIDTH_BACK = str.maketrans(":!?();,~", "：！？（）；，～")

# 括号里的内容常常就是萌娘的条目名：IS〈无限斯特拉托斯〉→《无限斯特拉托斯》
BRACKETED = re.compile(r"[〈【（(［\[]([^〉】）)］\]]{2,})[〉】）)］\]]")


def strip_symbols(t: str) -> str:
    return re.sub(r"\s+", " ", SYMBOLS.sub("", t)).strip()


def punct_variants(t: str) -> list[str]:
    return [x for x in (t.translate(WIDTH), t.translate(WIDTH_BACK)) if x != t]


def bracketed(t: str) -> list[str]:
    """取括号内容。⚠️ 同样受比例守卫约束 —— 否则
    「剧场版 魔法少女小圆 [前篇] 起始的物语」会产出候选「前篇」，
    与「Re」是同一类垃圾（短片段恰好撞上某个无关条目）。
    """
    out = []
    for m in BRACKETED.findall(t):
        s = m.strip()
        if len(s) >= CUT_MIN_LEN and len(s) / max(len(t), 1) >= CUT_MIN_RATIO:
            out.append(s)
    return out


def _strip_once(t: str) -> list[str]:
    out = []
    for p in PREFIXES:
        if t.startswith(p):
            out.append(t[len(p):].strip())
    for s in SUFFIXES:
        if t.endswith(s):
            out.append(t[: -len(s)].strip())
    for c in CUTS:
        i = t.find(c)
        if i > 1:
            left = t[:i].strip()
            if len(left) >= CUT_MIN_LEN and len(left) / max(len(t), 1) >= CUT_MIN_RATIO:
                out.append(left)
    return out


def strip_subtitle(t: str, rounds: int = 2) -> list[str]:
    """剥副标题，返回若干候选。

    ⚠️ **必须迭代，一轮不够。**「剧场版 魔法少女小圆 [前篇] 起始的物语」
       一轮只能去掉前缀**或**在 `[` 处截断，得到「剧场版 魔法少女小圆」；
       两轮才能拿到萌娘实际用的「魔法少女小圆」。
       候选是免费的（prop=info 一次 50 个），宁可多生成。
    """
    seen = {t}
    frontier = [t]
    out: list[str] = []
    for _ in range(rounds):
        nxt = []
        for x in frontier:
            for y in _strip_once(x):
                y = re.sub(r"\s+", " ", y).strip()
                if len(y) >= CUT_MIN_LEN and y not in seen:
                    seen.add(y)
                    out.append(y)
                    nxt.append(y)
        frontier = nxt
    return out


def load_gaps(top: int) -> list[dict]:
    """前 top 个系列里，**没有任何 chunk** 的那些，连同它们的全部已知名字。"""
    sql = """
    WITH roots AS (
        SELECT coalesce(series_root, subject_id) AS root, sum(fav_done) AS heat
          FROM anime_profile WHERE NOT nsfw GROUP BY 1
         ORDER BY heat DESC LIMIT %s
    ), gaps AS (
        SELECT r.root, r.heat FROM roots r
         WHERE NOT EXISTS (SELECT 1 FROM plot_chunk_scope s WHERE s.series_root = r.root)
    )
    SELECT g.root, g.heat,
           array_remove(array_agg(DISTINCT p.name_cn), NULL),
           array_remove(array_agg(DISTINCT p.name), NULL)
      FROM gaps g
      JOIN anime_profile p ON coalesce(p.series_root, p.subject_id) = g.root
     GROUP BY g.root, g.heat ORDER BY g.heat DESC
    """
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (top,))
        return [{"root": r[0], "heat": r[1], "names": [x for x in (r[2] + r[3]) if x]}
                for r in cur.fetchall()]


def build_variants(gaps: list[dict]) -> tuple[dict[str, str], dict[str, list[int]]]:
    """生成新候选。返回 (候选 → 类别 A/B, 候选 → 用到它的系列根)。"""
    kind: dict[str, str] = {}
    owner: dict[str, list[int]] = {}
    for g in gaps:
        base = set(g["names"])

        def add(v: str, k: str, root: int = 0, _b=base) -> None:
            if v and v not in _b:
                kind.setdefault(v, k)          # 已被判 A 的不降级
                owner.setdefault(v, []).append(root)

        for n in g["names"]:
            # ── A 类：同一部作品的不同写法，安全 ──────────────
            add(strip_symbols(n), "A", g["root"])
            for v in punct_variants(n):
                add(v, "A", g["root"])
            for v in punct_variants(strip_symbols(n)):
                add(v, "A", g["root"])

            # ── B 类：剥副标题 / 取括号内容，把衍生作指向母条目 ──
            for v in bracketed(n):
                add(v, "B", g["root"])
            for v in strip_subtitle(n):
                add(v, "B", g["root"])
            for v in strip_subtitle(strip_symbols(n)):
                add(v, "B", g["root"])
            # ⚠️ 标点归一后再剥副标题 —— 「Re：…雪之回忆」要先变半角冒号，
            #    才能剥出萌娘实际用的「Re:从零开始的异世界生活」。
            for pv in punct_variants(n):
                for v in strip_subtitle(pv):
                    add(v, "B", g["root"])
    return kind, owner


def apply_scope(conn, pairs: list[tuple[int, int]]) -> int:
    """(series_root, pageid) → 给该页所有 chunk 建 scope 行。"""
    if not pairs:
        return 0
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE tmp_fix (series_root int, pageid int) ON COMMIT DROP")
        cur.executemany("INSERT INTO tmp_fix VALUES (%s, %s)", pairs)
        cur.execute("""
            INSERT INTO plot_chunk_scope (series_root, chunk_id)
            SELECT f.series_root, c.chunk_id
              FROM tmp_fix f JOIN plot_chunk c ON c.pageid = f.pageid
            ON CONFLICT DO NOTHING
        """)
        n = cur.rowcount
    conn.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--top", type=int, default=3500, help="考察前 N 个系列")
    ap.add_argument("--delay", type=float, default=7.0, help="请求间隔秒数，别调低")
    ap.add_argument("--apply-a", action="store_true", help="应用 A 类（符号差异）映射")
    ap.add_argument("--apply-b", action="store_true", help="应用 B 类（副标题剥离）映射")
    # ⚠️ **应用不该重新解析。** 解析要打 40+ 次请求，而结果早就落在 json 里了。
    #    默认流程是「先跑一遍只报告 → 人工过一眼 → 用本模式应用」，
    #    中间那步人工审查正是这个脚本存在的理由（实测抓到过 Re→《哲学》）。
    ap.add_argument("--from-file", action="store_true",
                    help="从上次的 json 应用，不联网")
    ap.add_argument("--merge-titles", action="store_true",
                    help="把新条目并进 moegirl_titles.json，供 fetch_moegirl.py --reuse-titles 抓")
    args = ap.parse_args()

    if args.merge_titles:
        # ⚠️ 只并**尚未抓取**的条目。已抓的走 --from-file --apply-b 建映射即可，
        #    并进来虽然 do_fetch 会跳过（它按本地文件存在与否判断），但会白白
        #    污染 moegirl_titles.json 的语义 —— 那个文件记的是「抓过什么」。
        saved = json.loads(OUT.read_text(encoding="utf-8"))
        titles = json.loads(TITLE_MAP.read_text(encoding="utf-8"))
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pageid FROM moegirl_page")
            have = {r[0] for r in cur.fetchall()}
            cur.execute("""
                SELECT coalesce(series_root, subject_id), sum(fav_done)
                  FROM anime_profile GROUP BY 1
            """)
            heat = dict(cur.fetchall())
        added = 0
        for k, v in saved.items():
            if v["pageid"] in have or k in titles:
                continue
            titles[k] = {
                "title": v["title"], "pageid": v["pageid"],
                "lastrevid": v.get("lastrevid"), "length": v.get("length"),
                "matched_from": v["asked"], "heat": int(heat.get(int(k), 0)),
            }
            added += 1
        TITLE_MAP.write_text(json.dumps(titles, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        pages = len({v["pageid"] for k, v in saved.items() if v["pageid"] not in have})
        print(f"并入 {added} 条（{pages} 个不重复条目）→ {TITLE_MAP.name} 现有 {len(titles):,} 条")
        print("下一步：uv run --group etl python scripts/fetch_moegirl.py --reuse-titles")
        return 0

    if args.from_file:
        if not OUT.exists():
            print(f"✗ 缺少 {OUT}，先跑一次不带 --from-file 的解析", file=sys.stderr)
            return 1
        saved = json.loads(OUT.read_text(encoding="utf-8"))
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pageid FROM moegirl_page")
            have = {r[0] for r in cur.fetchall()}
        todo = [(int(k), v["pageid"]) for k, v in saved.items()
                if v["pageid"] in have
                and ((v["kind"] == "A" and args.apply_a)
                     or (v["kind"] == "B" and args.apply_b))]
        skipped = len(saved) - len(todo)
        print(f"从 {OUT.name} 读到 {len(saved)} 条，本次应用 {len(todo)} 条"
              f"（跳过 {skipped} 条：类别未选中，或页面尚未抓取）")
        if not todo:
            return 0
        with db.connect() as conn:
            n = apply_scope(conn, todo)
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(DISTINCT series_root) FROM plot_chunk_scope")
            roots = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM plot_chunk_scope")
            total = cur.fetchone()[0]
        print(f"✓ 新增 {n:,} 条 plot_chunk_scope"
              f"  ·  现共 {total:,} 行 / 覆盖 {roots:,} 个系列根")
        # ⚠️ 可逆：误应用了就删掉这批 series_root 的映射再重来
        print("  （要撤销：DELETE FROM plot_chunk_scope WHERE series_root = ANY(...)）")
        return 0

    gaps = load_gaps(args.top)
    print(f"前 {args.top:,} 个系列里没有语料的：{len(gaps):,} 个")

    kind, owner = build_variants(gaps)
    titles = sorted(kind)
    na = sum(1 for t in titles if kind[t] == "A")
    print(f"新候选标题 {len(titles):,} 个（A 类符号差异 {na} · B 类副标题 {len(titles)-na}）")
    print(f"约 {len(titles) // 50 + 1} 次请求 ≈ {(len(titles) // 50 + 1) * args.delay / 60:.1f} 分钟\n")

    with httpx.Client(headers={"User-Agent": UA}, timeout=60,
                      follow_redirects=True) as c:
        found = resolve_titles(c, titles, args.delay)

    # 已抓过的 pageid（库里有 chunk 的）
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pageid FROM moegirl_page")
        have = {r[0] for r in cur.fetchall()}

    # 每个系列取第一个命中（候选已按 A 优先排序不了，这里按 kind 排）
    best: dict[int, tuple[str, dict, str]] = {}
    for asked in sorted(found, key=lambda t: (kind[t], t)):
        for root in owner[asked]:
            if root not in best:
                best[root] = (asked, found[asked], kind[asked])

    fix_a = [(r, v[1]["pageid"]) for r, v in best.items()
             if v[2] == "A" and v[1]["pageid"] in have]
    fix_b = [(r, v[1]["pageid"]) for r, v in best.items()
             if v[2] == "B" and v[1]["pageid"] in have]
    new_pages = {v[1]["pageid"]: v for r, v in best.items() if v[1]["pageid"] not in have}

    print(f"\n=== 结果：{len(best):,} / {len(gaps):,} 个系列找到了条目 ===")
    print(f"  指向**已抓页面**（零抓取，直接建映射）：A 类 {len(fix_a)} · B 类 {len(fix_b)}")
    print(f"  指向**新页面**（需另行抓取）：{len(new_pages)} 个条目")

    heat = {g["root"]: g["heat"] for g in gaps}
    name = {}
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT subject_id, name_cn FROM anime_profile WHERE subject_id = ANY(%s)",
                    (list(best),))
        name = dict(cur.fetchall())

    print("\n  按热度前 20（→ 表示映射到哪个萌娘条目）：")
    for root, (asked, info, k) in sorted(best.items(), key=lambda x: -heat[x[0]])[:20]:
        tag = "已抓" if info["pageid"] in have else "新页"
        print(f"    [{k}·{tag}] heat={heat[root]:>7,}  {str(name.get(root))[:26]:28}"
              f" → 《{info['title']}》")

    OUT.write_text(json.dumps({
        str(r): {"asked": a, "kind": k, **i} for r, (a, i, k) in best.items()
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n映射已写入 {OUT.name}（{len(best)} 条）")
    print(f"⚠️ 其中 {len(new_pages)} 个新条目要抓正文，"
          f"约 {len(new_pages) * (args.delay + 2.5) / 60:.0f} 分钟")

    todo = (fix_a if args.apply_a else []) + (fix_b if args.apply_b else [])
    if todo:
        with db.connect() as conn:
            n = apply_scope(conn, todo)
        print(f"\n✓ 已建 {n:,} 条 plot_chunk_scope 映射")
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(DISTINCT series_root) FROM plot_chunk_scope")
            print(f"  覆盖系列根 {cur.fetchone()[0]:,} 个")
    elif fix_a or fix_b:
        print("\n（未应用。加 --apply-a / --apply-b 才会写库）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

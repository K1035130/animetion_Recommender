"""第 5 周评测 · B.1 可回答率题库（人工打分）。

🚨 **要回答的问题：覆盖率 ≠ 可回答率（I.9）。**

    GOSICK 有萌娘条目、有 16 条 chunk，在「覆盖作品 79.3% / 热度加权 97.3%」
    这个统计里算**已覆盖** —— 但问「结局是什么」答不了，因为萌娘主条目
    本身就没写结局（原始 HTML 里「结局」0 次）。
    ⇒ 97.3% 这个数字会把这类空洞整个盖住。

本脚本抽样出题、跑完整管道（检索 + 生成），产出一份**人工打分表**。

    build  →  docs/eval-answerability-sheet.md   （Kevin 填）
    score  →  读回填好的表，算指标

⚠️ **打分必须分成两问，且顺序不能反**（G.5f 那次我判错过一回）：
       第一问：答案**在不在**检索到的 chunk 里
       第二问：模型答得对不对
   不先看 chunk 就给模型打分，会系统性地**奖励幻觉、惩罚正确的拒答**。
   ⇒ 打分表里第一问的填空**排在模型回答之前**，物理上强制这个顺序。

⚠️ 题型里有两类是**故意放进来测路由的**，不计入可回答率：
     `播出时间`  Bangumi 结构化字段已有权威答案，E.4 判据② 明确不往 chunk 里存
                 ⇒ 正确行为是**拒答**（或由路由层去查库），不是编一个出来
     `关联作品`  正解是 /api/related 那条 SQL（I.3），/ask 按设计看不到别的作品
   这两类测的是「问了不该走 RAG 的问题会怎样」。

⚠️ 「结局」类问题跑 spoiler=True（模拟用户已确认要剧透），否则门控会把
   剧情结局那批 chunk 滤掉，测出来的是门控不是可回答率。

⚠️ 评测入口 allow_fallback=False（A.8 纪律 4）。只读，不写数据库。
   成本：每题 1 编码 + 1 rerank + 1 次 LLM ≈ 6 秒。

用法：
    uv run --group etl python scripts/eval_answerability.py build --n 60
    uv run --group etl python scripts/eval_answerability.py build --n 6 --out /tmp/x.md
    uv run --group etl python scripts/eval_answerability.py score
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import clients, db
from src import retrieve as R

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "docs" / "eval-answerability-sheet.md"
RAW = ROOT / "data" / "interim" / "eval_answerability_raw.json"

# 题型配比（总数按 --n 等比缩放）。
# ⚠️ 前五类计入可回答率，后两类只测路由 —— 见模块注释。
MIX: tuple[tuple[str, int, bool], ...] = (
    # (题型, 权重, 是否计入可回答率)
    ("剧情梗概", 12, True),
    ("结局",     12, True),
    ("角色是谁", 10, True),
    ("角色关系", 10, True),
    ("片头片尾",  8, True),
    ("播出时间",  4, False),
    ("关联作品",  4, False),
)

MIN_DONE = 3000          # 只问有热度的作品 —— 用户不会去问冷门作

# 🚨 **打分表必须展示 chunk 全文，不能截断。**
#    第一版设了 CHUNK_PREVIEW=170，实测 244 条里 104 条（43%）被截，共截掉 15,776 字 ——
#    而打分的第一问正是「答案在不在资料里」。答案若落在被截掉的部分，
#    打分人会标 retrieval:n，而模型（看到的是全文）答对了 → 被记成
#    **「未命中却给了答案 = 幻觉」**。⇒ 显示截断会系统性地制造假幻觉。
#    实测撞到的原例：【鲁道夫·冯·高登巴姆】全长 410 字，被截在第 170 字
#    「…把民主主义的银河」，而下一句正是答案「联邦改为专制主义的银河帝国，自任皇帝」。
#    ⚠️ 同族教训：I.8 ②「测试构造得不对，绿灯毫无意义」——
#       给打分人看的材料必须与模型看到的**完全一致**。
#
# 安全阀只对荒谬长度生效，且**明说还剩多少字**，不留一个含糊的省略号
#（现集最长 527 字，够不着）。
CHUNK_HARD_CAP = 1200


def sample_works(conn, k: int, seed: int) -> list[dict]:
    """抽有语料的作品，附带它们的角色（供角色类题目用）。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.subject_id, COALESCE(p.name_cn, p.name),
                   count(*) FILTER (WHERE c.character_id IS NOT NULL) AS n_char,
                   count(*) FILTER (WHERE c.source = 'moegirl')       AS n_moe,
                   count(*) FILTER (WHERE c.kind = 'songs')           AS n_song
              FROM anime_profile p
              JOIN plot_chunk_scope s ON s.series_root = p.subject_id
              JOIN plot_chunk c       ON c.chunk_id = s.chunk_id
             WHERE p.fav_done >= %s AND p.subject_id = p.series_root
             GROUP BY p.subject_id, COALESCE(p.name_cn, p.name)
             HAVING count(*) >= 3
             ORDER BY p.subject_id
        """, (MIN_DONE,))
        works = [{"root": r[0], "title": r[1], "n_char": r[2],
                  "n_moe": r[3], "n_song": r[4]} for r in cur.fetchall()]

    rnd = random.Random(seed)
    picked = rnd.sample(works, min(k, len(works)))

    # 每部作品取几个有 chunk 的角色名，供「角色是谁 / 角色关系」出题。
    #
    # ⚠️ **按简介总长度取，不要随机取。** 随机抽实测会抽出「梓2号」「海豚」
    #    「路人」「鳗鱼电力」这类占位/路人条目，出的题（「梓2号和海豚是什么关系？」）
    #    没人会问 —— 它们会拉低可回答率，但拉低的方式不反映真实用法。
    #    简介长度是现成的重要度代理：主角的 dump 简介明显更长（F.7b 同一观察）。
    with conn.cursor() as cur:
        for w in picked:
            cur.execute("""
                SELECT c.section
                  FROM plot_chunk_scope s
                  JOIN plot_chunk c ON c.chunk_id = s.chunk_id
                 WHERE s.series_root = %s AND c.character_id IS NOT NULL
                   AND c.section IS NOT NULL AND length(c.section) BETWEEN 2 AND 12
                 GROUP BY c.section
                 ORDER BY sum(length(c.text)) DESC
                 LIMIT 8
            """, (w["root"],))
            names = [r[0] for r in cur.fetchall()]
            rnd.shuffle(names)          # 8 个主要角色之间再随机，保证题目多样
            w["chars"] = names
    return picked


def make_questions(works: list[dict], n: int, seed: int) -> list[dict]:
    """按 MIX 配比出题。⚠️ 确定性：同一个 seed 必须给出同一批题。"""
    rnd = random.Random(seed + 1)
    total_w = sum(w for _, w, _ in MIX)
    out: list[dict] = []
    pool = list(works)
    rnd.shuffle(pool)
    i = 0

    def take() -> dict:
        nonlocal i
        w = pool[i % len(pool)]
        i += 1
        return w

    for kind, weight, counted in MIX:
        want = max(1, round(n * weight / total_w))
        made = 0
        guard = 0
        while made < want and guard < len(pool) * 3:
            guard += 1
            w = take()
            if kind == "剧情梗概":
                q = f"《{w['title']}》讲了什么故事？"
            elif kind == "结局":
                q = f"《{w['title']}》的结局是什么？"
            elif kind == "角色是谁":
                if not w["chars"]:
                    continue
                q = f"{w['chars'][0]}是谁？"
            elif kind == "角色关系":
                if len(w["chars"]) < 2:
                    continue
                q = f"{w['chars'][0]}和{w['chars'][1]}是什么关系？"
            elif kind == "片头片尾":
                q = f"《{w['title']}》的片头曲是什么？"
            elif kind == "播出时间":
                q = f"《{w['title']}》是什么时候播出的？"
            else:
                q = f"《{w['title']}》的原作者还做过什么作品？"
            out.append({"kind": kind, "counted": counted, "question": q,
                        "title": w["title"], "root": w["root"],
                        "n_moe": w["n_moe"], "n_song": w["n_song"],
                        "n_char": w["n_char"],
                        # 结局类放开剧透门控，否则测的是门控不是可回答率
                        "spoiler": kind == "结局"})
            made += 1
    return out


def build(args) -> int:
    conn = db.connect()
    try:
        works = sample_works(conn, max(args.n, 20), args.seed)
        qs = make_questions(works, args.n, args.seed)
        print(f"出题 {len(qs)} 道，覆盖 {len({q['root'] for q in qs})} 部作品\n")
        for i, q in enumerate(qs, 1):
            ans = R.ask(conn, q["question"], spoiler=q["spoiler"],
                        allow_fallback=False)      # ⚠️ 评测锁死主力模型
            q["state"] = ans.state.value
            q["answer"] = ans.text or ""
            q["chunks"] = [{"id": c.chunk_id, "section": c.section,
                            "text": c.text, "score": c.score,
                            "pinned": c.pinned, "source": c.source}
                           for c in ans.chunks]
            q["meta"] = ans.meta
            print(f"  [{i:2d}/{len(qs)}] {q['kind']:5} {q['state']:10} "
                  f"{q['question'][:34]}")
    finally:
        conn.close()

    RAW.parent.mkdir(parents=True, exist_ok=True)
    RAW.write_text(json.dumps(qs, ensure_ascii=False, indent=2), encoding="utf-8")
    out = Path(args.out) if args.out else SHEET
    out.write_text(render(qs), encoding="utf-8")
    print(f"\n打分表  {out}")
    print(f"原始数据 {RAW}")
    print("\n填完后跑： uv run --group etl python scripts/eval_answerability.py score")
    return 0


HEADER = """# 可回答率打分表（B.1）

> 🚨 **打分规则 —— 两问，顺序不能反。**
>
> 每道题有两个填空，`?` 改成对应字母即可，**其余内容不要动**（脚本靠格式解析）。
>
> **第一问 `retrieval:`** —— 只看上面列出的资料，**答案在不在里面**？
>   `y` = 在（哪怕只有部分）· `n` = 不在
>   ⚠️ 它排在模型回答**之前**是有意的：先判资料，再看回答。
>      反过来会系统性地奖励幻觉、惩罚正确的拒答（G.5f 那次我就判错过一回）。
>
> **第二问 `answer:`** —— 模型这次表现如何？判据只有一条：**它给没给出实质回答？**
>   `r` = 没给（只说「资料中没有提到」之类，没有实质内容）
>   `y` = 给了，且内容正确
>   `n` = 给了，但内容错（含**张冠李戴**：把别人的信息安到问的对象头上，这类最危险）
>   ⚠️ **先说「资料中没有提到」、接着又补了一段实质内容** —— 那算**给了**，
>      按内容对错记 `y`/`n`，**不是 `r`**。
>      📌 这段内容是**从资料零碎拼出来的**还是**模型自己的知识**，是另一个维度，
>         不影响 y/n/r 的判定 —— 觉得可疑就在 `note:` 里记一句，别改字母。
>         （实测第 11 题看着像"用自己的知识"，回查 chunk 发现每句都有出处。
>          **判越界要逐句找出处，不能靠文风判断。**）
>
> **四种组合的读法**（脚本自动交叉统计，你只管如实填）：
>
> | | `answer: r` | `answer: y` | `answer: n` |
> |---|---|---|---|
> | **`retrieval: y`** 资料里有 | ⚠️ 命中却拒答＝生成失败（I.2 ② 被噪声稀释那类） | ✅ 理想 | ❌ 答错 |
> | **`retrieval: n`** 资料里没有 | ✅ **满分**：没有就说没有 | 🚨 无出处作答 | 🚨 编造 |
>
> ⇒ **「资料里没有 + 模型说没有」填 `r`，不要填 `y`。**
>    它是**正确行为**，但不是「答对了」—— 用户并没有拿到答案。
>    两者在报告里必须分开：**前者是语料覆盖的问题，后者才是模型的问题**，
>    而这恰恰是本次评测（覆盖率 ≠ 可回答率）要区分的东西。
>
> `note:` 可留空，遇到有意思的失败形态就记一句。
>
> 📌 **资料是全文照登，没有省略。** 若某条末尾是「……」，那是**语料原文本身**
>    就那样收尾的（库内 1.53% 的 chunk 如此，多为简介留悬念），
>    **不是这里截断的、也不代表后面还有内容没给你看**。
>    ⚠️ 这一条很重要：初版曾把长 chunk 截到 170 字再补个「…」，
>    而 43% 的 chunk 因此被截 —— 答案若落在截掉的部分，你会标 `retrieval: n`
>    而模型（看到全文）答对了，于是被记成「幻觉」。**给你看的必须与模型看到的一致。**
>
> 📌 **`播出时间` / `关联作品` 两类不计入可回答率**，它们测的是路由：
>    前者的权威答案在 Bangumi 结构化字段里（E.4 判据② 明确不往 chunk 存），
>    后者的正解是 `/api/related` 那条 SQL（I.3）—— **这两类的正确行为是拒答**。
>
> 填完跑：`uv run --group etl python scripts/eval_answerability.py score`

"""


def render(qs: list[dict]) -> str:
    lines = [HEADER]
    for i, q in enumerate(qs, 1):
        lines.append(f"---\n\n## [{i}] {q['kind']}　·　{q['question']}\n")
        tags = [f"作用域语料：角色 {q['n_char']} · 萌娘 {q['n_moe']} · 歌曲 {q['n_song']}",
                f"状态 `{q['state']}`"]
        if q["spoiler"]:
            tags.append("**已放开剧透门控**")
        if not q["counted"]:
            tags.append("**不计入可回答率（测路由）**")
        lines.append("　".join(tags) + "\n")

        if q["chunks"]:
            lines.append(f"**检索到的 {len(q['chunks'])} 条资料**：\n")
            for j, c in enumerate(q["chunks"], 1):
                # ⚠️ 全文照登。末尾若出现「……」那是**语料原文自带的**
                #    （库内 1.53% 的 chunk 本来就以省略号收尾），不是这里截的。
                txt = c["text"].replace("\n", " ")
                if len(txt) > CHUNK_HARD_CAP:
                    txt = (txt[:CHUNK_HARD_CAP]
                           + f"〔⚠️ 本条还有 {len(c['text']) - CHUNK_HARD_CAP} 字未显示〕")
                sc = f"{c['score']:.3f}" if c["score"] is not None else "  —  "
                pin = " 📌" if c["pinned"] else ""
                sec = f"【{c['section']}】" if c["section"] else ""
                lines.append(f"{j}. `{sc}`{pin} {sec}{txt}")
            lines.append("")
        else:
            lines.append("**没有检索到任何资料**（管道短路，未调用 LLM）\n")

        lines.append("▼ **第一问**：上面的资料里有没有这道题的答案？（y / n）\n")
        lines.append("```")
        lines.append("retrieval: ?")
        lines.append("```\n")
        lines.append("<details><summary>▼ 判完第一问再展开：模型的回答</summary>\n")
        lines.append("> " + (q["answer"] or "（无 —— 管道短路）"
                             ).replace("\n", "\n> ") + "\n")
        lines.append("</details>\n")
        lines.append("▼ **第二问**：模型答得如何？（y 对 / n 错 / r 拒答）\n")
        lines.append("```")
        lines.append("answer: ?")
        lines.append("note:")
        lines.append("```\n")
    return "\n".join(lines)


BLOCK = re.compile(r"^## \[(\d+)\] (\S+?)　", re.MULTILINE)
STATE = re.compile(r"状态 `(\w+)`")

# 题型 → 是否计入可回答率。从 MIX 派生，别再写第二份。
COUNTED = {k: c for k, _, c in MIX}


def score(args) -> int:
    """读回填好的打分表并算指标。

    ⚠️ **只依赖打分表本身，不读 data/interim 里的原始 JSON。**
       那个目录不入 git，而打分表在 docs/ 会被提交 —— 若把两者绑在一起，
       换机器后打分表还在、指标却算不出来了。
       与「不入 git 的文件参与打分链路是定时炸弹」（B 节 series_root.json）同一条。
       ⇒ 题型、状态全部从表里解析，`counted` 由 MIX 派生。
    """
    sheet = Path(args.sheet) if args.sheet else SHEET
    if not sheet.exists():
        print(f"找不到 {sheet} —— 先跑 build")
        return 1
    text = sheet.read_text(encoding="utf-8")

    blocks = list(BLOCK.finditer(text))
    graded, ungraded = [], 0
    for m, nxt in zip(blocks, [*blocks[1:], None], strict=True):
        seg = text[m.end():nxt.start() if nxt else len(text)]
        r = re.search(r"^retrieval:\s*(\S*)", seg, re.MULTILINE)
        a = re.search(r"^answer:\s*(\S*)", seg, re.MULTILINE)
        rv = (r.group(1) if r else "?").lower()
        av = (a.group(1) if a else "?").lower()
        if rv not in ("y", "n") or av not in ("y", "n", "r"):
            ungraded += 1
            continue
        kind = m.group(2)
        st = STATE.search(seg)
        graded.append({"kind": kind, "counted": COUNTED.get(kind, True),
                       "state": st.group(1) if st else "?",
                       "retrieval": rv, "answer": av})

    print(f"已打分 {len(graded)} / {len(blocks)} 道"
          f"{f'（{ungraded} 道还没填）' if ungraded else ''}\n")
    if not graded:
        return 1

    core = [g for g in graded if g["counted"]]
    route = [g for g in graded if not g["counted"]]

    def pct(xs, pred) -> str:
        n = sum(1 for x in xs if pred(x))
        return f"{n:3d}/{len(xs):<3d} {n / len(xs):6.1%}" if xs else "  —"

    print("=== 可回答率（前五类题）===")
    print(f"  检索命中（答案在资料里）   {pct(core, lambda g: g['retrieval'] == 'y')}")
    print(f"  端到端答对                 {pct(core, lambda g: g['answer'] == 'y')}")
    hit = [g for g in core if g["retrieval"] == "y"]
    miss = [g for g in core if g["retrieval"] == "n"]
    print(f"  ├ 命中时答对（生成质量）   {pct(hit, lambda g: g['answer'] == 'y')}")
    print(f"  └ 未命中时正确拒答         {pct(miss, lambda g: g['answer'] == 'r')}")
    print(f"  🚨 未命中却给了答案（幻觉） {pct(miss, lambda g: g['answer'] in 'yn')}")

    print("\n=== 分题型 ===")
    print(f"  {'题型':10} {'n':>3}  {'检索命中':>8} {'答对':>8}")
    for kind in [k for k, _, _ in MIX]:
        xs = [g for g in graded if g["kind"] == kind]
        if not xs:
            continue
        h = sum(1 for g in xs if g["retrieval"] == "y") / len(xs)
        y = sum(1 for g in xs if g["answer"] == "y") / len(xs)
        print(f"  {kind:10} {len(xs):3d}  {h:8.1%} {y:8.1%}")

    if route:
        print("\n=== 路由类（不计入可回答率，正确行为是拒答）===")
        print(f"  正确拒答  {pct(route, lambda g: g['answer'] == 'r')}")
        print(f"  🚨 编了个答案 {pct(route, lambda g: g['answer'] != 'r')}")

    print("\n=== 状态分布 ===")
    for k, v in Counter(g["state"] for g in graded).most_common():
        print(f"  {k:12} {v:3d}")
    return 0


def rerender(args) -> int:
    """只重排版，不重跑管道。

    ⚠️ 改了展示格式（比如取消截断）时用它 —— 题目、检索结果、模型回答
       全部从 RAW 原样读出，**零 API 调用，且保证与已生成的那份完全一致**。
    🚨 会覆盖打分表，**已填的答案会丢** —— 所以先检查有没有填过。
    """
    out = Path(args.out) if args.out else SHEET
    if out.exists():
        filled = len(re.findall(r"^retrieval: [ynYN]\s*$",
                                out.read_text(encoding="utf-8"), re.MULTILINE))
        if filled and not args.force:
            print(f"🚨 {out} 里已经填了 {filled} 道 —— 重渲染会覆盖掉。"
                  f"\n   确认要丢弃请加 --force，或先把它复制一份。")
            return 1
    qs = json.loads(RAW.read_text(encoding="utf-8"))
    out.write_text(render(qs), encoding="utf-8")
    print(f"重排版完成（{len(qs)} 道，未调用任何 API）  {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="出题 + 跑管道 + 生成打分表")
    b.add_argument("--n", type=int, default=60)
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--out", default=None)
    b.set_defaults(fn=build)
    r = sub.add_parser("render", help="只按当前格式重排版，不重跑管道")
    r.add_argument("--out", default=None)
    r.add_argument("--force", action="store_true", help="已填过也强制覆盖")
    r.set_defaults(fn=rerender)
    s = sub.add_parser("score", help="读回填好的打分表并算指标")
    s.add_argument("--sheet", default=None)
    s.set_defaults(fn=score)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    # ⚠️ 收尾必须在 finally 里：异常路径同样要放掉 httpx 连接池。
    #    见 src/clients.py —— 这四个脚本此前只关了 Neon 连接。
    try:
        _code = main()
    finally:
        clients.close_all()
    raise SystemExit(_code)

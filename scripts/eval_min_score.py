"""第 5 周评测 · 把 `MIN_SCORE` 和「查询串含作品名」两条悬案一次扫完。

🚨 **要回答的两个问题**（week5-eval-report.md §4.2 / §4.3 提出，修法都还没实现）：

    A 地板   「片头片尾」8/8 全失败。songs chunk 每次都被正确召回、排层内第 1，
             但 rerank 分只有 0.003–0.028，被 `MIN_SCORE=0.05` 全部砍掉。
             ⇒ 地板该改成多少，还是该换一种判据？
    B 召回   「剧情梗概」查询里的作品名是噪声：元信息章节（广播剧版/游戏版）
             反复提作品名所以词面匹配高，真正的剧情简介只讲故事、不提作品名。
             ⇒ 剥掉作品名之后，剧情章节能不能进召回池？

⚠️ **两个修法必须一起评估**（§4.3 末尾）：只修 B，救回来的 chunk 仍会被 A 的地板砍掉。

────────────────────────────────────────────────────────────────
① 🚨 **rerank 的分数不是逐 (query, doc) 对独立的 —— 假设过，被自己的探针推翻。**

   本想「两个变体的池子取并集打一次分」省一半调用、还能做同分对比。
   `--probe-rerank` 实测（12 条文档，一批 8 条 / 两批重叠 4 条，**各重复 5 次**）：

                        中位        max        读法
       同批重发       0.00e+00   7.96e-04   大多数时候逐位确定，**偶尔**跳
       跨批同文档     5.30e-04   6.10e-04   **每次都不同**
       （同批分数范围 0.0100 ~ 0.0345，供比例参考）

   ⇒ **两个效应都真实存在，且同量级**：
       · 批次组成（padding：批内补齐到最长序列）→ **必然**改分，~5e-4
       · 服务端连续批处理（和别的用户拼批，A.7 同一现象）→ **偶发**，~8e-4

   🚨 **单次试验会给出错误结论。** 第一轮同批重发恰好返回 0.000e+00，
      我据此写下"reranker 对固定批次是确定的、3e-4 全归因于 padding"——
      重复 5 次就翻案了。**这与 A.7 那次一字不差**（原文：「第一次探测时
      前者恰好返回 0.000e+00，那是碰巧不是确定」）。
      ⇒ **凡是"差异为零"的观测，必须重复若干次才能当成结论。**

   📌 **对本脚本的处理**：不分批、不取并集，两个变体各自 rerank 自己的池子 ——
      **这恰好就是生产环境真实发生的事**（上线后 fix B 的池子就是 strip 池）。
      每题两次 rerank、每次 ≤93 条 < MAX_DOCS=100，一次调用装得下。

   🚨 **对地板决策的直接影响（本次扫描最重要的副产物）**：
      分数噪声约 **5e-4 ~ 8e-4**，而 §4.2 里被砍掉的 songs chunk 得分
      **0.0032 ~ 0.0282** —— 噪声最高能占最低分的 **25%**。
      ⇒ **任何 1e-3 量级的绝对地板都落在噪声里**，调不出稳定行为。
        这是"换判据"（相对地板 / 类型豁免）而不是"把 0.05 调小"的独立论据。

② **`MIN_SCORE` 实际只控制一件事：`others` 的前缀取多长。**
   `_apply_pin_reserve` 里 `others` 已按 rerank 降序，而
   `[c for c in others if score >= floor][:room]` 恒等于取 `others` 的一个前缀
   （降序序列里过线的恰好是开头那一段），`MIN_KEEP` 兜底取的也是前缀。
   ⇒ **降低地板是纯增量的**：0.05 时进了最终集的 chunk，更低的地板下必然还在。
   🚨 **这条直接推出一个不用再测的结论**：降低地板**不可能**让 §4 里已经命中的题
      变成未命中 —— **检索侧的代价恒为零，全部代价都在生成侧**（I.2 ② 的上下文稀释）。
      ⇒ 本脚本负责检索侧（便宜、客观、可穷举），生成侧要另跑一轮 60 题。

────────────────────────────────────────────────────────────────
⚠️ **题目来自 `data/interim/eval_answerability_raw.json`，不重新抽样。**
   必须与 Kevin 打过分的那 60 题**逐题对应**，否则人工标签对不上。
   ⚠️ 那个文件不入 git —— 缺了先跑
   `eval_answerability.py build --n 60 --seed 0`（题目由 seed 决定，是确定性的）。

⚠️ 只读库，不写。不调 LLM。成本：每题 2 次编码 + 2 次 rerank。

用法：
    uv run --group etl python scripts/eval_min_score.py pool --probe-rerank
    uv run --group etl python scripts/eval_min_score.py sweep      # 离线，不花钱
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import clients, db, embed, textproc
from src import rerank as rr
from src import retrieve as R

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "interim" / "eval_answerability_raw.json"
SHEET = ROOT / "docs" / "eval-answerability-sheet.md"
OUT = ROOT / "data" / "interim" / "eval_min_score.json"

# 问「片头/片尾」时答案的载体是 songs chunk —— 这是**客观**判据，不需要人工标签。
# §4.2 已逐条 trace 确认：songs chunk 每次都在池子里、排层内第 1，
# 唯一的问题是被地板砍掉。
#
# ⚠️ **直接用线上那条正则，不要在这里再抄一份。** 评测和线上判"这是不是
#    OP/ED 题"必须是同一个函数 —— 抄一份就会漂移，而漂移的后果是
#    "评测里 5/7、线上不是那 7 道题"，且不报错。
#    （与「查询词必须走同一套 jieba」「建库和查询必须同一个 embedding」同一条纪律。）
SONGS_Q = R.SONGS_QUERY

# 问「讲了什么故事」时答案的载体是剧情章节。
# ⚠️ 这是**代理判据不是标准答案**：靠 section 名匹配，会漏掉写在别处的剧情。
#    所以只用来看"名次有没有前进"，不用来算命中率。
#
# 🚨 **只能匹配章节路径的叶子，不能匹配整条路径。**
#    第一版写成整条路径匹配，于是《一人之下》的
#    `作品介绍 > 漫画番外篇` 因为前缀里有"作品介绍"被当成了剧情章节，
#    还正好排第 1，报出一个漂亮但错误的"#7 → #1"。
#    而 §4.3 说的真正那条是 `作品介绍 > 剧情简介` —— **差别全在叶子上。**
#    ⇒ 与 E.8「不能靠读起来通不通顺判断解析对错」同族：
#      代理判据挑中了错的东西时，输出照样像模像样。
PLOT_SEC = re.compile(r"剧情|簡介|简介|故事|概要|梗概")


def plot_leaf(section: str | None) -> bool:
    """章节路径的**叶子**是不是剧情章节。`A > B > C` 只看 C。"""
    if not section:
        return False
    return bool(PLOT_SEC.search(section.split(">")[-1].strip()))


# ============================================================
# 变体 B · 剥掉查询串里的作品名
# ============================================================
#
# 🚨 **结论：净收益 0，不实现。这段代码留作可复测的负结果。**
#
#    §4.3 报告的两个例子（一人之下 #14→#2、头脑特工队 #3→#1）**都是真的**，
#    但它们是挑出来的。55 题里 30 题可比：
#
#        名次前进 13 · 不变 9 · 后退 8
#        进入 QUOTA_SERIES：救回 +3 · 掉出 −3 · **净 +0**
#
#    ⚠️ 掉出配额的三题（银河英雄传说 #6→外、魔法科高校 #2→外、
#       在地下城 #10→外）与救回的三题（一人之下、中国奇谭、进击的巨人）
#       在作用域大小上**没有可分的规律**（最大的 8 个作用域恰好 4↑/4↓）——
#       所以也做不成"大作用域才剥"这种条件规则。
#
# 🚨 **顺带记一个方法学错误，它差点让这个无效改动通过。**
#    第一版挑目标 chunk 用的是 `min(cands, key=lr_strip)` ——
#    **按剥名后的名次挑目标**，等于"哪条在新方案里表现好就拿哪条来证明它好"。
#    那一版报出「净救回 +4」；改成与变体无关的判据（最长的那条剧情章节）后
#    立刻变成 **+0**。⚠️ 与 I.8 ②「测试构造得不对，绿灯毫无意义」同族，
#    而且这类偏差**只往有利方向偏**，最难被自己发现。
#
# 📌 为什么保留而不删：语料会变（阶段 06 抓角色页会大幅改写系列层），
#    到时候重跑 `pool` + `sweep` 就能复测，删掉反而要重写。
#    与 sql/008 译文备份表"保留不删"同一条理由。


def _norm_offsets(s: str) -> tuple[str, list[int]]:
    """逐字做 norm_name，同时记下归一化后每个字符来自原串的哪个下标。

    ⚠️ **逐字归一化不总是等于整串归一化**（NFKC 对组合序列可能跨字符合并）。
       所以调用方必须校验重建出来的串与 `norm_name(s)` 相等，不等就放弃剥离 ——
       宁可不剥，也不要在错位的下标上乱切。
    """
    parts, idx = [], []
    for i, ch in enumerate(s):
        n = textproc.norm_name(ch)
        parts.append(n)
        idx.extend([i] * len(n))
    return "".join(parts), idx


# 剥完至少要剩这么多个归一化字符，否则放弃。
# 「《一人之下》讲了什么故事？」→「讲了什么故事」6 个，够；
# 而用户只打了一个作品名（「《紫罗兰永恒花园》」）剥完是 0 个 ——
# 那种查询本来就该由整段语义去匹配，剥成空串会让召回退化成随机。
MIN_STRIPPED = 4

# 空的书名号 / 各式括号对 —— 剥掉名字后留下的空壳。
_EMPTY_BRACKETS = re.compile(r"[《〈「『\[(（【]\s*[》〉」』\])）】]")


def strip_scope_title(question: str, res: R.Resolution) -> str:
    """剥掉问句里指向**当前作用域**的作品名。

    📌 立论：作用域已由 resolve 钉死（`WHERE s.series_root = %(root)s`），
       池子里每一条都属于这部作品 ⇒ 作品名在这里的区分度恒等于零。
       ⚠️ **这个前提没了它就是错的**：跨作品检索（G.1 路径③）里作品名是
          最强的信号，绝不能剥。
    ⚠️ 只剥作品名，不剥角色名 —— 角色名在作用域内仍有区分度
       （「三笠和艾伦是什么关系」剥掉两个名字就什么都不剩了）。

    找不到可剥的、剥完太短、或下标校验没过 —— 一律**原样返回**。
    """
    if res.state is not R.State.OK or res.series_root is None:
        return question
    targets = {m.text for m in res.mentions
               if m.entity_type == "subject" and m.series_root == res.series_root}
    if not targets:
        return question

    norm, idx = _norm_offsets(question)
    if norm != textproc.norm_name(question):
        return question                     # 逐字/整串归一化不一致，放弃

    cuts: list[tuple[int, int]] = []
    for name in targets:
        at = norm.find(name)
        while at != -1:
            cuts.append((idx[at], idx[at + len(name) - 1] + 1))
            at = norm.find(name, at + 1)
    if not cuts:
        return question

    keep = [True] * len(question)
    for lo, hi in cuts:
        for i in range(lo, hi):
            keep[i] = False
    out = _EMPTY_BRACKETS.sub("", "".join(ch for i, ch in enumerate(question)
                                          if keep[i]))
    if len(textproc.norm_name(out)) < MIN_STRIPPED:
        return question
    return out


# ============================================================
# 一、重跑检索，落全量分数
# ============================================================

def score_pool(question: str, pool: list[R.Chunk]) -> dict[int, float]:
    """给一个池子打分 —— **完整复制生产的那一次调用**，不分批、不合并。

    🚨 分批会改分数（模块注释里那次实测），所以这里断言装得下：
       最坏 pinned 40（MAX_PINNED）+ 召回 53 = 93 < MAX_DOCS=100，
       与 `MAX_PINNED` 注释里那条余量论证是同一个不变式。
    """
    if len(pool) > rr.MAX_DOCS:
        raise RuntimeError(
            f"池子 {len(pool)} 条超过 MAX_DOCS={rr.MAX_DOCS} —— "
            f"分批会改分数，这里宁可失败也不静默降级")
    ranked = rr.rerank(question, [c.text for c in pool], top_n=len(pool))
    return {pool[idx].chunk_id: sc for idx, sc in ranked}


def layer_of(c: R.Chunk) -> str:
    return ("char" if c.character_id is not None
            else "songs" if c.kind == "songs" else "series")


def layer_ranks(pool: list[R.Chunk]) -> dict[int, int]:
    """层内名次。recall() 的返回在层内保持 rn 序，所以数一遍就是名次。"""
    seen: dict[str, int] = {}
    out: dict[int, int] = {}
    for c in pool:
        lay = layer_of(c)
        seen[lay] = seen.get(lay, 0) + 1
        out[c.chunk_id] = seen[lay]
    return out


def run_one(conn, q: dict) -> dict:
    """一道题：解析 → 两个变体各召回一次 → 并集打一次分。"""
    res = R.resolve(conn, q["question"])
    if res.state is not R.State.OK:
        return {"skip": res.state.value}

    stripped = strip_scope_title(q["question"], res)
    spoiler = bool(q.get("spoiler"))

    pinned = R.pinned_chunks(conn, res.character_ids, spoiler=spoiler)
    pin_ids = {c.chunk_id for c in pinned}
    pools: dict[str, list[R.Chunk]] = {}
    for tag, text in (("orig", q["question"]), ("strip", stripped)):
        qvec = embed.embed_query(text, retries=R.REQUEST_EMBED_RETRIES,
                                 timeout=R.REQUEST_EMBED_TIMEOUT)
        pools[tag] = R.recall(conn, res.series_root, qvec, spoiler=spoiler)

    # 每个变体各自组池 + 各自 rerank —— **逐步复刻生产**（含 pinned 合并顺序）。
    # ⚠️ rerank 的 query 恒为**原问句**：变体 B 只换召回查询，不换 rerank
    #    和 LLM 看到的东西（§4.3 实测剥名前后 rerank 分不变）。
    scores: dict[str, dict[int, float]] = {}
    variant_pool: dict[str, list[R.Chunk]] = {}
    for tag in ("orig", "strip"):
        merged = list(pinned)
        seen = set(pin_ids)
        for c in pools[tag]:
            if c.chunk_id not in seen:
                seen.add(c.chunk_id)
                merged.append(c)
        variant_pool[tag] = merged
        scores[tag] = score_pool(q["question"], merged)

    lr = {tag: layer_ranks(pools[tag]) for tag in pools}
    ids = {tag: {c.chunk_id for c in variant_pool[tag]} for tag in variant_pool}

    # 汇总所有出现过的 chunk（两个变体的并集），每条带**两套分数**。
    allc: dict[int, R.Chunk] = {}
    for tag in ("orig", "strip"):
        for c in variant_pool[tag]:
            allc.setdefault(c.chunk_id, c)

    return {
        "skip": None,
        "question": q["question"],
        "stripped": stripped,
        "kind": q["kind"],
        "counted": q.get("counted", True),
        "root": res.series_root,
        "title": res.title or "",
        "n_pinned": len(pinned),
        "chunks": [{
            "id": c.chunk_id, "section": c.section, "kind": c.kind,
            "source": c.source, "layer": layer_of(c),
            "pinned": c.chunk_id in pin_ids,
            # ⚠️ 两套分数，各自来自各自那一次 rerank 调用。
            #    不在这里取平均或择一 —— 那等于把批次效应抹平，
            #    而我们刚刚实测它是真实存在的。
            "score_orig": scores["orig"].get(c.chunk_id),
            "score_strip": scores["strip"].get(c.chunk_id),
            "in_orig": c.chunk_id in ids["orig"],
            "in_strip": c.chunk_id in ids["strip"],
            "lr_orig": lr["orig"].get(c.chunk_id),
            "lr_strip": lr["strip"].get(c.chunk_id),
            "text": c.text,
        } for c in allc.values()],
    }


def probe_rerank(conn, repeats: int = 5) -> None:
    """把「rerank 分数受什么影响」测清楚 —— 两个对照 × 多次重复，缺一不可。

    🚨 **两个设计要点都是踩出来的**：
       ① 只测跨批会**错误归因**：拿到 3e-4 会写成"服务端抖动"。
          要有"同批重发"这条对照才分得开 padding 与连续批处理。
       ② **只测一次会得到相反的结论**：首轮同批重发恰好 0.000e+00，
          据此写下"固定批次是确定的"，重复 5 次立刻翻案（max 7.96e-04）。
          与 A.7 那次一模一样 ⇒ **"差异为零"的观测必须重复。**

    本脚本不分批、不合并，所以这只记录事实、不当闸门。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT text FROM plot_chunk WHERE vec IS NOT NULL "
                    "ORDER BY chunk_id LIMIT 12")
        docs = [r[0] for r in cur.fetchall()]
    q = "这部作品讲了什么故事？"
    def S(ds):
        return {i: sc for i, sc in rr.rerank(q, ds, top_n=len(ds))}

    same, cross = [], []
    for _ in range(repeats):
        a1, a2, b = S(docs[:8]), S(docs[:8]), S(docs[4:])
        same.append(max(abs(a1[i] - a2[i]) for i in a1))
        cross.append(max(abs(a1[4 + j] - b[j]) for j in range(4)))
    rng = S(docs[:8])
    print(f"  rerank 探针 ×{repeats}（分数范围 "
          f"{min(rng.values()):.4f}~{max(rng.values()):.4f}）")
    print(f"    同批重发    中位 {statistics.median(same):.2e}  max {max(same):.2e}")
    print(f"    跨批同文档  中位 {statistics.median(cross):.2e}  max {max(cross):.2e}")
    print("    ⇒ 噪声 ~1e-3 量级：**1e-3 以下的绝对地板调不出稳定行为**")


def cmd_pool(args) -> int:
    if not RAW.exists():
        print(f"找不到 {RAW}\n先跑：uv run --group etl python "
              f"scripts/eval_answerability.py build --n 60 --seed 0")
        return 1
    qs = json.loads(RAW.read_text(encoding="utf-8"))
    if args.limit:
        qs = qs[:args.limit]      # 先跑通小样本再全量（第 15 节原则 5）
    conn = db.connect()
    try:
        if args.probe_rerank:
            probe_rerank(conn)
        recs = []
        for i, q in enumerate(qs, 1):
            rec = run_one(conn, q)
            rec["idx"] = i
            recs.append(rec)
            if rec["skip"]:
                print(f"  [{i:2d}/{len(qs)}] {q['kind']:5} 跳过（{rec['skip']}）")
            else:
                new = sum(1 for c in rec["chunks"]
                          if c["in_strip"] and not c["in_orig"])
                print(f"  [{i:2d}/{len(qs)}] {q['kind']:5} 池 {len(rec['chunks']):3d} "
                      f"剥名新增 {new:2d}  {q['question'][:28]}")
    finally:
        # 🚨 **三个 httpx client 都要显式关。**
        #    模块级 client 是惰性单例，进程退出时不保证被回收 ——
        #    现有 eval 脚本正是漏了这一步（close_client 有定义、无调用）。
        conn.close()
        clients.close_all()

    OUT.write_text(json.dumps(recs, ensure_ascii=False), encoding="utf-8")
    print(f"\n落盘 {OUT}（{len(recs)} 题）\n下一步：eval_min_score.py sweep")
    return 0


# ============================================================
# 二、离线扫描
# ============================================================

def pick(chunks: list[dict], *, keep, final: int = R.FINAL,
         reserve: int = R.PIN_RESERVE, min_keep: int = R.MIN_KEEP,
         seat: int | None = None) -> list[dict]:
    """`_apply_pin_reserve` 的可插拔版本 —— 地板判据由 keep 决定。

    ⚠️ 结构必须与 `R._apply_pin_reserve` 逐字对应；sweep 的第 0 步会用现值
       做等价性断言（与 tests/test_parity.py 同一条纪律：两套实现靠测试锁住，
       不靠纪律）。
    """
    order = sorted(chunks, key=lambda c: -c["score"])
    # 保底席位与 pinned 同等对待 —— **占座，不是放行**（见 keep_songs 注释）。
    pins = [c for c in order if c["pinned"] or c["id"] == seat][:reserve]
    pin_ids = {c["id"] for c in pins}
    others = [c for c in order if c["id"] not in pin_ids]
    room = max(0, final - len(pins))
    kept = [c for c in others if keep(c)][:room]
    if len(kept) < min_keep:
        kept = others[:min(min_keep, room)]
    return sorted(pins + kept, key=lambda c: -c["score"])[:final]


def in_pool(rec: dict, variant: str) -> list[dict]:
    """按变体取出该变体池子里的 chunk，并把 `score` 绑成**该变体自己那套分数**。

    ⚠️ 不能跨变体借分数：两套分数来自两次不同的 rerank 调用，
       批次组成不同（padding 效应实测 3.2e-4）。借用等于把两个实验混在一起。
    """
    key = f"in_{variant}"
    skey = f"score_{variant}"
    return [{**c, "score": c[skey]} for c in rec["chunks"]
            if (c["pinned"] or c[key]) and c[skey] is not None]


def keep_abs(floor: float):
    return lambda c: c["score"] >= floor


def keep_rel(frac: float, top1: float):
    return lambda c: c["score"] >= top1 * frac


def songs_seat(question: str, pool: list[dict]) -> int | None:
    """问的是 OP/ED 时，songs 层第 1 那条的 chunk_id；否则 None。"""
    if not SONGS_Q.search(question):
        return None
    songs = [c for c in pool if c["layer"] == "songs"]
    if not songs:
        return None
    return min(songs, key=lambda c: c["lr_orig"] or 999)["id"]


def keep_songs(floor: float, question: str, pool: list[dict]):
    """按类型**豁免地板**：问的是 OP/ED，且这条是 songs 层第 1 → 不受地板约束。

    🚨 **豁免 ≠ 占座 —— 这正是 I.2 ① 那条教训的原样复发。**
       `kept = [c for c in others if keep(c)][:room]` 仍按分数降序取前缀，
       所以一条被豁免但分数极低的 chunk 排在 others 末尾，
       **只要前面过线的够 room 条，它照样被截掉**。
       ⇒ 这一行只用来量出"光豁免不够"；占座版走 `pick(..., seat=)`。
    """
    seat = songs_seat(question, pool)
    return lambda c: c["score"] >= floor or c["id"] == seat


def read_labels() -> dict[int, dict]:
    """从打分表读回人工标签，按题号对齐。"""
    if not SHEET.exists():
        return {}
    text = SHEET.read_text(encoding="utf-8")
    blocks = list(re.finditer(r"^## \[(\d+)\] (\S+?)　", text, re.MULTILINE))
    out = {}
    for m, nxt in zip(blocks, [*blocks[1:], None], strict=True):
        seg = text[m.end():nxt.start() if nxt else len(text)]
        r = re.search(r"^retrieval:\s*(\S*)", seg, re.MULTILINE)
        a = re.search(r"^answer:\s*(\S*)", seg, re.MULTILINE)
        out[int(m.group(1))] = {
            "retrieval": (r.group(1) if r else "?").lower(),
            "answer": (a.group(1) if a else "?").lower(),
        }
    return out


def cmd_sweep(args) -> int:
    if not OUT.exists():
        print(f"找不到 {OUT} —— 先跑 pool")
        return 1
    recs = [r for r in json.loads(OUT.read_text(encoding="utf-8")) if not r["skip"]]
    lab = read_labels()
    print(f"可用 {len(recs)} 题（打分表里 {len(lab)} 题）\n")

    # ── 0. 等价性断言：pick(现值) 必须与线上 _apply_pin_reserve 一致 ──
    bad = []
    for rec in recs:
        pool = in_pool(rec, "orig")
        mine = [c["id"] for c in pick(pool, keep=keep_abs(R.MIN_SCORE))]
        theirs = [c.chunk_id for c in R._apply_pin_reserve(
            [R.Chunk(chunk_id=c["id"], section=c["section"], text=c["text"],
                     kind=c["kind"], source=c["source"], character_id=None,
                     spoiler_level=0, score=c["score"], pinned=c["pinned"])
             for c in sorted(pool, key=lambda c: -c["score"])], R.FINAL)]
        if mine != theirs:
            bad.append(rec["idx"])
    print("=== 0. 模拟器与线上实现的等价性 ===")
    print(f"  {len(recs) - len(bad)}/{len(recs)} 题一致"
          f"{'  ✅' if not bad else f'  🚨 不一致：{bad} —— 下面的数字全部作废'}\n")
    if bad:
        return 1

    # ── 1. 前缀性质：降低地板是纯增量吗 ──
    viol = [r["idx"] for r in recs
            if not {c["id"] for c in pick(in_pool(r, "orig"),
                                          keep=keep_abs(R.MIN_SCORE))}
            <= {c["id"] for c in pick(in_pool(r, "orig"), keep=keep_abs(0.0))}]
    print("=== 1. 「降低地板是纯增量」（性质 ②）===")
    print(f"  0.05 的最终集 ⊆ 0.00 的最终集：{len(recs) - len(viol)}/{len(recs)}"
          f"{'  ✅ 检索侧代价恒为零' if not viol else f'  🚨 反例 {viol}'}\n")

    # ── 2. 地板扫描 ──
    songs_qs = [r for r in recs if SONGS_Q.search(r["question"])]
    base = {r["idx"]: {c["id"] for c in pick(in_pool(r, "orig"),
                                             keep=keep_abs(R.MIN_SCORE))}
            for r in recs}

    print("=== 2. 地板扫描 ===")
    print(f"  songs 客观判据：{len(songs_qs)} 道 OP/ED 题，"
          f"「最终集里有没有 songs chunk」")
    print(f"  {'判据':24} {'平均条数':>8} {'songs命中':>10} {'较现值新增/题':>14}")

    def row(name: str, keeper, use_seat: bool = False) -> None:
        tot = new = 0
        for r in recs:
            pool = in_pool(r, "orig")
            top1 = max((c["score"] for c in pool), default=0.0)
            st = songs_seat(r["question"], pool) if use_seat else None
            got = pick(pool, keep=keeper(r, top1, pool), seat=st)
            tot += len(got)
            new += len({c["id"] for c in got} - base[r["idx"]])
        hit = 0
        for r in songs_qs:
            pool = in_pool(r, "orig")
            top1 = max((c["score"] for c in pool), default=0.0)
            st = songs_seat(r["question"], pool) if use_seat else None
            hit += any(c["layer"] == "songs"
                       for c in pick(pool, keep=keeper(r, top1, pool), seat=st))
        mark = "  ← 现值" if name == f"绝对 {R.MIN_SCORE}" else ""
        print(f"  {name:24} {tot / len(recs):8.2f} "
              f"{hit:4d}/{len(songs_qs):<5d} {new / len(recs):14.2f}{mark}")

    for f in (0.0, 0.005, 0.01, 0.02, 0.03, R.MIN_SCORE, 0.08, 0.10):
        row(f"绝对 {f}", lambda r, t, p, f=f: keep_abs(f))
    for fr in (0.02, 0.05, 0.10, 0.20):
        row(f"相对 top1×{fr}", lambda r, t, p, fr=fr: keep_rel(fr, t))
    row(f"绝对{R.MIN_SCORE} + songs 豁免",
        lambda r, t, p: keep_songs(R.MIN_SCORE, r["question"], p))
    row(f"绝对{R.MIN_SCORE} + songs 保底席位",
        lambda r, t, p, f=R.MIN_SCORE: keep_abs(f), use_seat=True)

    # songs 命中不满分时，先分清是"地板砍的"还是"池子里根本没有" ——
    # 后者是语料覆盖问题，地板怎么调都救不回来，混在一起会低估修法的效果。
    no_songs = [r["idx"] for r in songs_qs
                if songs_seat(r["question"], in_pool(r, "orig")) is None]
    print(f"\n  ⚠️ 池子里**根本没有 songs chunk** 的题：{no_songs or '无'}"
          f"　—— 语料覆盖问题，不是地板问题")
    print()

    # ── 3. 剥作品名对召回的影响 ──
    print("=== 3. 剥作品名：剧情章节的层内名次（配额 "
          f"QUOTA_SERIES={R.QUOTA_SERIES}）===")
    print(f"  {'题':>3} {'题型':6} {'作品':14} {'章节':20} {'原查询':>7} {'剥名后':>7}")
    tot = fwd = same = 0
    rescued = lost = 0
    for r in recs:
        if r["question"] == r["stripped"]:
            continue
        cands = [c for c in r["chunks"]
                 if c["layer"] == "series" and plot_leaf(c["section"])]
        if not cands:
            continue
        # 🚨 **目标 chunk 必须用与变体无关的判据选。**
        #    初版写的是 `min(cands, key=lr_strip)` —— 按**剥名后的名次**挑目标，
        #    等于"哪条在新方案里表现好就拿哪条来证明新方案好"，
        #    系统性偏袒 strip。⚠️ 与 I.8 ②「测试构造得不对，绿灯毫无意义」同族，
        #    而且这一处**只会往有利方向偏**，最容易蒙混过关。
        #    ⇒ 改用「最长的那条剧情章节」：与两个变体都无关，
        #      且长度本来就是"哪条是真正的简介"的合理代理
        #      （eval_answerability.py 挑角色时用的是同一条判据）。
        c = max(cands, key=lambda c: len(c["text"]))
        a, b = c["lr_orig"] or 999, c["lr_strip"] or 999
        tot += 1
        fwd += b < a
        same += b == a
        was_in = c["in_orig"] and a <= R.QUOTA_SERIES
        now_in = c["in_strip"] and b <= R.QUOTA_SERIES
        rescued += now_in and not was_in
        lost += was_in and not now_in
        if a != b:
            fa = f"#{a}" if c["in_orig"] else "外"
            fb = f"#{b}" if c["in_strip"] else "外"
            print(f"  {r['idx']:3d} {r['kind']:6} {r['title'][:12]:14} "
                  f"{(c['section'] or '')[:18]:20} {fa:>7} {fb:>7}"
                  f"{'  ← 救回' if now_in and not was_in else ''}"
                  f"{'  ← 🚨 掉出配额' if was_in and not now_in else ''}")
    # 🚨 **必须同时数"掉出配额"。** 初版只数救回却把它标成"净救回"，
    #    而实测确实有掉出的题（银英 #6 → 外）—— 单向计数在有反向案例时
    #    会把结论说反，这与 B 节那条「只保证输入相同不保证语义相同」同族。
    print(f"\n  可比 {tot} 题：名次前进 {fwd} · 不变 {same} · "
          f"后退 {tot - fwd - same}")
    print(f"  进入配额：救回 +{rescued} · 掉出 −{lost} · "
          f"**净 {rescued - lost:+d} 题**")

    # ── 4. 待人工标注的增量 ──
    miss = [r for r in recs if lab.get(r["idx"], {}).get("retrieval") == "n"]
    delta = sum(len({c["id"] for c in pick(in_pool(r, "orig"), keep=keep_abs(0.0))}
                    - base[r["idx"]]) for r in miss)
    print("\n=== 4. 需要增量人工标注的量 ===")
    print(f"  0.05 判 retrieval:n 的题 {len(miss)} 道，"
          f"地板降到 0 后新进最终集 {delta} 条 chunk")
    print("  ⚠️ 这些 chunk Kevin 没看过 —— 含不含答案必须单独标，不能从现有标签推。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool", help="重跑检索，落全量分数（无 LLM）")
    p.add_argument("--probe-rerank", action="store_true",
                   help="测一遍 rerank 分数受批次影响多少（记录事实）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 题（试运行）")
    p.set_defaults(fn=cmd_pool)
    s = sub.add_parser("sweep", help="离线扫参数，不花钱")
    s.set_defaults(fn=cmd_sweep)
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

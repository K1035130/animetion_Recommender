"""流程 B 找番（G.1 路径①）· BM25 该不该并进语义腿，怎么并。

**要回答的问题**：`/api/find` 的语义腿（`anime_profile.vec`）之外，
要不要再加一条 BM25 腿（`search_tsv`），加的话怎么融合？

沿用 eval_hyde.py 的方法（tag 是天然 held-out 标签，理由见那边的模块注释），
但这里必须多防一层：**BM25 腿直接命中 `search_tsv` 的 B 档就是 tag 名本身**
（load_profiles.py 的注释：BM25 两档权重 = 标题/别名 + 题材 tag）。
用 tag 当标签去评「一条按 tag 建的索引」，天然循环论证的风险比 HyDE 那次更高
（HyDE 两条腿全部只见过 summary，从没见过 tag，不存在这个问题）。

⇒ 每条查询标出 **tag_literal**：分词后是否有 token 字面等于某个 held-out
tag。分组报告——不分组的整体数字会被"查询里直接写了标签名"的那部分
稀释/放大，看不出 BM25 对真正的**转述**查询有没有用。

⚠️ 两个已知 bug，本脚本顺手验证修法：
① `plainto_tsquery` 是 AND 语义，多词自然语言查询几乎必然全灭
   （原型实测 5/5 条描述性查询返回 0 行）。改用 OR + 停用词。
② 停用词表是本脚本新写的最小集合，**只覆盖这 20 条查询观察到的噪声词**，
   不是语言学意义上完整的停用词表 —— 不要挪到别处直接当通用词表用。

⚠️ 不做序列折叠（不按 series_root 去重）—— 与 eval_hyde 的 pool 一致，
   为了让两边可比；folding 是生产端的 UX 决定，这里不测。

用法：
    uv run --group etl python scripts/eval_find.py
    uv run --group etl python scripts/eval_find.py --limit 3 --json out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from src import clients, db, embed, tag_rules
from src.tagvec import keep_tags
from src.textproc import tokenize

# 与 eval_hyde.py 相同的 20 条查询 + tag 标签（held-out 相关性标注）。
# 特意不 import scripts.eval_hyde —— scripts/ 不是包，且各 eval 脚本
# 各管各的查询集是本项目的既有约定（eval_crosslang.py 同样自带一份）。
QUERIES: tuple[tuple[str, frozenset[str]], ...] = tuple(
    (q, frozenset(ts)) for q, ts in [
        ("让人捧腹大笑的喜剧", {"搞笑", "喜剧"}),
        ("剑与魔法的冒险故事", {"奇幻"}),
        ("激烈的打斗与对决", {"战斗"}),
        ("未来科技与宇宙飞船", {"科幻", "SF"}),
        ("女孩子之间的恋爱", {"百合", "GL"}),
        ("青涩的校园恋爱故事", {"恋爱", "纯爱"}),
        ("发生在学校社团里的故事", {"校园"}),
        ("平淡温馨的生活片段", {"日常"}),
        ("温暖人心、看完很放松", {"治愈"}),
        ("充满斗志、永不放弃", {"热血"}),
        ("穿越到另一个世界重新开始", {"异世界", "穿越"}),
        ("巨大机器人驾驶与战斗", {"萝卜", "机战"}),
        ("体育竞技与社团训练", {"运动"}),
        ("演艺圈里成长的偶像团体", {"偶像"}),
        ("以乐器演奏为主题的故事", {"音乐"}),
        ("扑朔迷离的离奇事件", {"悬疑", "猎奇"}),
        ("变身战斗的魔法少女", {"魔法少女"}),
        ("侦探破解案件", {"推理"}),
        ("战场上的军队与士兵", {"战争"}),
        ("以古代历史为背景", {"历史", "武侠"}),
    ]
)

TOP_K = 10
RRF_K = 60

# 本脚本观察到的噪声 token（虚词/泛义词），不是通用停用词表 —— 见模块注释②。
STOP_WORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "有", "没有", "有没有", "番", "动画",
    "动漫", "故事", "关于", "讲", "那种", "这种", "一个", "什么", "哪些",
    "推荐", "想看", "看", "部", "很", "但", "着", "又", "还", "来", "去",
    "地", "得", "会", "能", "为", "以", "而", "或", "及", "等", "被",
})


def load_pool(conn) -> tuple[np.ndarray, np.ndarray, list[set[str]]]:
    """(ids, mat, 每部作品的干净 tag 集合)。**排除 nsfw**——找番默认不推。"""
    with conn.cursor(binary=True) as cur:
        cur.execute("""
            SELECT subject_id, vec, tags
              FROM anime_profile
             WHERE vec IS NOT NULL AND NOT nsfw
             ORDER BY subject_id
        """)
        rows = cur.fetchall()

    vocab = keep_tags()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.zeros((len(rows), embed.DIM), dtype=np.float32)
    tags: list[set[str]] = []
    for i, r in enumerate(rows):
        v = r[1]
        mat[i] = (v.to_numpy() if hasattr(v, "to_numpy") else np.asarray(v)
                  ).astype(np.float32)
        clean = set()
        for t in (r[2] or []):
            n = tag_rules.normalize(t["name"])
            if n in vocab and tag_rules.classify(n) == "KEEP":
                clean.add(n)
        tags.append(clean)
    n = np.linalg.norm(mat, axis=1, keepdims=True)
    return ids, mat / np.where(n == 0, 1.0, n), tags


def rank_of(mat: np.ndarray, q: np.ndarray, k: int) -> np.ndarray:
    sims = mat @ q
    idx = np.argpartition(-sims, k)[:k]
    return idx[np.argsort(-sims[idx], kind="stable")]


def bm25_rank(conn, ids_index: dict[int, int], query: str, k: int) -> np.ndarray:
    """BM25 腿：OR + 去停用词，返回行号（对齐 ids_index），不足 k 条就少给。

    ⚠️ 用 websearch_to_tsquery 而不是 plainto_tsquery —— 前者认识 OR 语法，
       后者只有 AND，是本脚本要验证修掉的那个 bug（模块注释①）。
    """
    toks = [t for t in tokenize(query).split() if t and t not in STOP_WORDS]
    if not toks:
        return np.array([], dtype=np.int64)
    q = " OR ".join(toks)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.subject_id, ts_rank(p.search_tsv, qq) AS rk
              FROM anime_profile p, websearch_to_tsquery('simple', %s) qq
             WHERE p.search_tsv @@ qq AND NOT p.nsfw AND p.vec IS NOT NULL
             ORDER BY rk DESC, p.fav_done DESC LIMIT %s
        """, (q, k))
        rows = cur.fetchall()
    return np.array([ids_index[sid] for sid, _ in rows if sid in ids_index],
                    dtype=np.int64)


def metrics(order: np.ndarray, rel: np.ndarray, k: int) -> tuple[float, float]:
    """(P@k, NDCG@k)。order 可能短于 k（BM25 常见），按实际长度算增益、
    但 IDCG 固定按 k 折满——排位不足本身就该被扣分，不能把分母也缩短。"""
    top = order[:k]
    gains = np.zeros(k, dtype=np.float64)
    if len(top):
        gains[: len(top)] = rel[top].astype(np.float64)
    disc = 1.0 / np.log2(np.arange(2, k + 2))
    idcg = disc.sum()
    return float(gains.mean()), float((gains * disc).sum() / idcg)


def rrf(*orders: np.ndarray, k: int) -> np.ndarray:
    score: dict[int, float] = {}
    for o in orders:
        for rank, row in enumerate(o):
            score[int(row)] = score.get(int(row), 0.0) + 1.0 / (RRF_K + rank + 1)
    return np.array(sorted(score, key=lambda r: -score[r])[:k], dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    queries = QUERIES[: args.limit] if args.limit else QUERIES
    legs = ("semantic", "bm25", "rrf")

    conn = db.connect()
    try:
        ids, mat, tags = load_pool(conn)
        ids_index = {int(sid): i for i, sid in enumerate(ids)}
        print(f"候选池（非 nsfw、有向量）  {len(ids):,} 部\n")

        acc: dict[str, list[tuple[float, float]]] = {leg: [] for leg in legs}
        acc_literal: dict[str, list[tuple[float, float]]] = {leg: [] for leg in legs}
        acc_para: dict[str, list[tuple[float, float]]] = {leg: [] for leg in legs}
        bm25_empty = 0
        rows_out = []

        for qi, (q, want) in enumerate(queries, 1):
            rel = np.array([bool(t & want) for t in tags])
            n_rel = int(rel.sum())

            toks = set(tokenize(q).split()) - STOP_WORDS
            tag_literal = bool(toks & want)

            v = embed.embed_query(q)
            o_sem = rank_of(mat, v, TOP_K)
            o_bm = bm25_rank(conn, ids_index, q, TOP_K)
            if len(o_bm) == 0:
                bm25_empty += 1
            o_rrf = rrf(o_sem, o_bm, k=TOP_K) if len(o_bm) else o_sem

            orders = {"semantic": o_sem, "bm25": o_bm, "rrf": o_rrf}
            line = {"query": q, "tags": sorted(want), "n_relevant": n_rel,
                   "tag_literal": tag_literal, "bm25_hits": len(o_bm)}
            for leg in legs:
                p, nd = metrics(orders[leg], rel, TOP_K)
                acc[leg].append((p, nd))
                (acc_literal if tag_literal else acc_para)[leg].append((p, nd))
                line[leg] = {"p@10": p, "ndcg@10": nd}
            rows_out.append(line)

            tag = "字面命中" if tag_literal else "转述"
            print(f"[{qi:2d}/{len(queries)}] {q}  （相关 {n_rel} 部 · {tag} · "
                  f"bm25={len(o_bm)} 条）")
            print("        " + "  ".join(
                f"{leg}: P={line[leg]['p@10']:.2f} N={line[leg]['ndcg@10']:.2f}"
                for leg in legs))
    finally:
        conn.close()

    def summarize(label: str, a: dict[str, list[tuple[float, float]]]) -> None:
        n = len(next(iter(a.values()), []))
        print(f"\n{'='*62}\n{label}（n={n}）")
        print(f"{'腿':10} {'P@10':>10} {'NDCG@10':>10}")
        for leg in legs:
            if not a[leg]:
                continue
            p = float(np.mean([x for x, _ in a[leg]]))
            nd = float(np.mean([x for _, x in a[leg]]))
            print(f"{leg:10} {p:10.3f} {nd:10.3f}")

    summarize("全体", acc)
    summarize("字面命中 tag 的查询（BM25 的天然优势区，解读时打折扣）", acc_literal)
    summarize("转述查询（无字面 tag，BM25 与语义腿公平对照）", acc_para)

    print(f"\nBM25 腿返回空结果的查询数：{bm25_empty}/{len(queries)}")
    n_para = len(acc_para["rrf"])
    if n_para:
        wins_para = sum(1 for i in range(n_para)
                        if acc_para["rrf"][i][1] > acc_para["semantic"][i][1])
        print(f"转述查询里 rrf 优于纯语义的有 {wins_para}/{n_para} 条")
    print("⚠️ 绝对值受 tag 覆盖不全影响被低估；腿间比较才是本测的结论。")
    print("⚠️ 「字面命中」分组下 bm25/rrf 的高分是设计使然（用户打的词本来就",
         "该字面命中），不是过拟合；只有「转述」分组能回答",
         "「BM25 对语义检索有没有增量价值」。")

    if args.json:
        args.json.write_text(json.dumps({
            "top_k": TOP_K, "rrf_k": RRF_K,
            "embed_fingerprint": embed.fingerprint(),
            "pool_size": len(ids),
            "bm25_empty": bm25_empty,
            "summary": {
                grp: {leg: {"p@10": float(np.mean([p for p, _ in a[leg]])) if a[leg] else None,
                           "ndcg@10": float(np.mean([n for _, n in a[leg]])) if a[leg] else None}
                     for leg in legs}
                for grp, a in (("all", acc), ("tag_literal", acc_literal),
                              ("paraphrase", acc_para))
            },
            "per_query": rows_out,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写出 {args.json}")
    return 0


if __name__ == "__main__":
    try:
        _code = main()
    finally:
        clients.close_all()
    raise SystemExit(_code)

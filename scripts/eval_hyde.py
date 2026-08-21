"""第 5 周评测 · B.2 HyDE 正式对照（G.5d 挂了两周的那条待办）。

**要回答的问题：HyDE 该不该默认开？**

此前只有 3 条查询的证据，而且两次结论相反：
    G.5c（日文语料时）  「关于组乐队的动画」基线 ❌ 披头士纪录片 / HyDE ✅ 轻音少女
    G.5d（转中文之后）  同一条查询基线 ✅ BanG Dream! / HyDE ❌ 蓝色管弦乐
G.5b 自己写过「三条查询不足以定质量排序」。本脚本用 20 条查询 + 自动标注重跑。

🔑 **自动标注怎么来的（本脚本成立的关键）**：
   `anime_profile.vec` 只编码 `summary`，**从没见过 tags**（A.10 的纪律：
   拼 tags 会让 embedding 这条 baseline 秘密变成混合体）。
   ⇒ **tag 是一份天然的 held-out 相关性标签** —— 用它评 summary-embedding 检索，
      不存在自我印证。这正好把 A.10 那条当初为 ablation 定的纪律变成了评测资产。

⚠️ **tag 标签的已知局限，报告里必须写明**：
   ① 用户打的 tag 覆盖不全（一部讲美食的番可能没人打「美食」）
      ⇒ **P@10 的绝对值被系统性低估**，只能用来做腿与腿之间的比较。
   ② 各腿面对的是同一份标签，所以**相对比较是公平的** —— 这才是我们要的。

四条腿（**一次 LLM 调用，三次编码**）：
    base    原查询 → embed_query
    hyde_q  HyDE 假想简介 → embed_query    （加 instruct 前缀）
    hyde_d  HyDE 假想简介 → embed_documents（不加前缀）
    rrf     base 与较优的那条 hyde 按名次融合（G.2：跨空间只能按名次融合）

📌 **hyde_q vs hyde_d 是 src/llm.py 的 hyde() docstring 里明确挂着的未决开关**
   （「假想文档该走 embed_documents 还是 embed_query 目前没有定论」，
   而 Qwen3 是非对称编码，两者 cos 仅 0.797）。顺手一并测掉。

⚠️ **评测入口一律 allow_fallback=False**（A.8 纪律 4）：fallback 只允许出现在
   线上演示路径，否则同一份评测两次可能落在不同模型上，数字不可复现。

⚠️ 只读，不写数据库。成本：20 次 LLM + 60 次编码，¥0.01 量级。

用法：
    uv run --group etl python scripts/eval_hyde.py
    uv run --group etl python scripts/eval_hyde.py --limit 3 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from src import clients, db, embed, llm, tag_rules
from src.textproc import keep_tags

# 查询集：(自然语言查询, 判定相关的 tag 集合)
#
# ⚠️ **措辞刻意不直接用 tag 词本身**（能避则避）—— 否则测的是关键词匹配
#    而不是语义泛化。少数几条绕不开（如「魔法少女」本身就是那个说法）。
# ⚠️ 一个题材有多个近义 tag 时全部列入（萝卜/机战），否则会把正确结果判成错。
# ⚠️ 改这张表 = 换口径，改完必须重跑全部对照。
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
RRF_K = 60          # 标准取值；名次融合对它不敏感


def load_pool(conn) -> tuple[np.ndarray, np.ndarray, list[set[str]]]:
    """(ids, mat, 每部作品的干净 tag 集合)。**排除 nsfw** —— 找番路径默认不推。"""
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
    """相似度前 k 的行号，按相似度降序。"""
    sims = mat @ q
    idx = np.argpartition(-sims, k)[:k]
    return idx[np.argsort(-sims[idx], kind="stable")]


def metrics(order: np.ndarray, rel: np.ndarray, k: int) -> tuple[float, float]:
    """(P@k, NDCG@k)。二元相关性；相关文档数远超 k，故 IDCG 取满。"""
    top = order[:k]
    gains = rel[top].astype(np.float64)
    disc = 1.0 / np.log2(np.arange(2, k + 2))
    idcg = disc.sum()
    return float(gains.mean()), float((gains * disc).sum() / idcg)


def rrf(*orders: np.ndarray, k: int) -> np.ndarray:
    """名次融合（G.2：两个向量空间的分数不可比，只能按名次融合）。"""
    score: dict[int, float] = {}
    for o in orders:
        for rank, row in enumerate(o):
            score[int(row)] = score.get(int(row), 0.0) + 1.0 / (RRF_K + rank + 1)
    return np.array(sorted(score, key=lambda r: -score[r])[:k], dtype=np.int64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条查询（试运行）")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    queries = QUERIES[:args.limit] if args.limit else QUERIES

    conn = db.connect()
    try:
        ids, mat, tags = load_pool(conn)
    finally:
        conn.close()           # ⚠️ 后面是长耗时的 LLM 阶段，别握着 Neon 连接
    print(f"候选池（非 nsfw、有向量）  {len(ids):,} 部\n")

    legs = ("base", "hyde_q", "hyde_d", "rrf")
    acc: dict[str, list[tuple[float, float]]] = {leg: [] for leg in legs}
    rows_out = []
    served = None

    for qi, (q, want) in enumerate(queries, 1):
        rel = np.array([bool(t & want) for t in tags])
        n_rel = int(rel.sum())

        v_base = embed.embed_query(q)
        doc, served = llm.hyde(q, allow_fallback=False)   # ⚠️ 评测锁死主力模型
        v_hq = embed.embed_query(doc)
        v_hd = embed.embed_documents([doc])[0]

        o = {"base": rank_of(mat, v_base, TOP_K),
             "hyde_q": rank_of(mat, v_hq, TOP_K),
             "hyde_d": rank_of(mat, v_hd, TOP_K)}
        # RRF 用 hyde_q 那条腿（与线上若要开 HyDE 的形态一致）
        o["rrf"] = rrf(o["base"], o["hyde_q"], k=TOP_K)

        line = {"query": q, "tags": sorted(want), "n_relevant": n_rel}
        for leg in legs:
            p, nd = metrics(o[leg], rel, TOP_K)
            acc[leg].append((p, nd))
            line[leg] = {"p@10": p, "ndcg@10": nd}
        rows_out.append(line)

        print(f"[{qi:2d}/{len(queries)}] {q}   （相关 {n_rel} 部）")
        print("        " + "  ".join(
            f"{leg}: P={line[leg]['p@10']:.2f} N={line[leg]['ndcg@10']:.2f}"
            for leg in legs))

    print("\n" + "=" * 62)
    print(f"{'腿':10} {'P@10':>10} {'NDCG@10':>10}   {'相对 base':>12}")
    base_p = float(np.mean([p for p, _ in acc["base"]]))
    for leg in legs:
        p = float(np.mean([x for x, _ in acc[leg]]))
        nd = float(np.mean([x for _, x in acc[leg]]))
        print(f"{leg:10} {p:10.3f} {nd:10.3f}   {(p - base_p) * 100:+11.1f} pp")

    wins = sum(1 for i in range(len(queries))
               if acc["hyde_q"][i][1] > acc["base"][i][1])
    print(f"\nhyde_q 在 {wins}/{len(queries)} 条查询上 NDCG 优于 base")
    print("⚠️ 绝对值受 tag 覆盖不全影响被低估；腿间比较才是本测的结论。")

    if args.json:
        args.json.write_text(json.dumps({
            "top_k": TOP_K, "rrf_k": RRF_K,
            "embed_fingerprint": embed.fingerprint(),
            "llm": llm.descriptor(served) if served else None,
            "pool_size": len(ids),
            "summary": {leg: {"p@10": float(np.mean([p for p, _ in acc[leg]])),
                              "ndcg@10": float(np.mean([n for _, n in acc[leg]]))}
                        for leg in legs},
            "per_query": rows_out,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写出 {args.json}")
    return 0


if __name__ == "__main__":
    # ⚠️ 收尾必须在 finally 里：异常路径同样要放掉 httpx 连接池。
    #    见 src/clients.py —— 这四个脚本此前只关了 Neon 连接。
    try:
        _code = main()
    finally:
        clients.close_all()
    raise SystemExit(_code)

"""第 5 周评测 · B.4 点名类检索的自动标注评测。

**要回答的问题**：`MIN_SCORE=0.05` 和召回宽度 50 这两个常量站得住吗？
现值分别是拿 **10 条 / 5 条**查询定的 —— 而 `EMB_TOL` 的教训就是
小样本定阈值、扩样后余量只剩 1.3 倍（B 节）。

🔑 **自动标注怎么来的**：问「X 是谁」时，标准答案**客观可判** ——
   就是 `character_id = X` 的那几条 chunk。不需要人来判相关性。
   ⇒ 可以从 `alias` × `plot_chunk` 自动生成几百道题，把两个常量扫出真曲线。

⚠️ **只覆盖点名类，不覆盖描述性问题**（后者仍需人工标注）。
   两者互补，与 G.6 末尾「alias 负责点了名的，召回+rerank 负责描述性的」是同一条分工。

⚙️ **每题只 rerank 一次，参数扫描靠离线模拟**：
   cross-encoder 的分数是 (query, doc) 对的属性，与批次无关 ——
   所以拿最宽的召回打一次分，再按不同 (召回宽度, MIN_SCORE, final) 从这批分数里
   筛，结果与真跑一遍完全一致。**否则扫描要打上千次 API。**

⚠️ 只读，不写数据库。成本：每题 1 次编码 + 1 次 rerank ≈ 2.5 秒。

用法：
    uv run --group etl python scripts/eval_retrieval_named.py --n 10      # 试运行
    uv run --group etl python scripts/eval_retrieval_named.py --n 150 --json out.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, embed
from src import rerank as rr
from src import retrieve as R

# 宽召回：拿它打一次分，再往窄了模拟。
# ⚠️ 三层之和 + pinned 必须 ≤ rerank.MAX_DOCS(100)，否则抛 RerankError。
WIDE = {"quota_char": 50, "quota_series": 12, "quota_songs": 4}

# 题目只从**有一定热度**的作品里取 —— 用户不会去问冷门作的路人角色。
MIN_DONE = 2000


def sample_questions(conn, n: int, seed: int) -> list[dict]:
    """自动生成点名题。每题 = (问句, 期望作用域集合, 标准答案 chunk 集合)。"""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.character_id,
                   max(c.section)                     AS name,
                   array_agg(DISTINCT c.chunk_id)     AS gold,
                   array_agg(DISTINCT s.series_root)  AS roots
              FROM plot_chunk c
              JOIN plot_chunk_scope s ON s.chunk_id = c.chunk_id
              JOIN anime_profile p    ON p.subject_id = s.series_root
             WHERE c.source = 'bangumi_char'
               AND c.section IS NOT NULL
               AND c.vec IS NOT NULL
               AND c.spoiler_level = 0
               AND p.fav_done >= %s
               AND EXISTS (SELECT 1 FROM alias a
                            WHERE a.entity_type = 'character'
                              AND a.character_id = c.character_id)
             GROUP BY c.character_id
             HAVING count(*) >= 1
             ORDER BY c.character_id
        """, (MIN_DONE,))
        rows = cur.fetchall()

    # ⚠️ 确定性抽样：固定种子 + 先按 character_id 排序，
    #    否则两次评测跑在不同题目上，数字不可比。
    rnd = random.Random(seed)
    picked = rnd.sample(rows, min(n, len(rows)))
    return [{"character_id": r[0], "name": r[1],
             "question": f"{r[1]}是谁？",
             "gold": set(r[2]), "roots": set(r[3])}
            for r in picked]


def run_one(conn, q: dict) -> dict | None:
    """跑到 rerank 为止，返回带分数的完整候选序列（不截断、不套地板）。"""
    res = R.resolve(conn, q["question"])
    out: dict = {"state": res.state.value,
                 "series_root": res.series_root,
                 "scope_ok": res.series_root in q["roots"]}
    if res.state is not R.State.OK or not out["scope_ok"]:
        return out

    pinned = R.pinned_chunks(conn, res.character_ids)
    qvec = embed.embed_query(q["question"])
    pool = R.recall(conn, res.series_root, qvec, **WIDE)

    seen = {c.chunk_id for c in pinned}
    merged = pinned + [c for c in pool if c.chunk_id not in seen]
    if len(merged) > rr.MAX_DOCS:            # 保 pinned，砍召回的尾巴
        merged = merged[:rr.MAX_DOCS]

    # 层内名次：模拟更窄的配额时用它筛（recall 的返回在层内保持 rn 序）
    layer_rank: dict[int, int] = {}
    counter: dict[str, int] = {}
    for c in pool:
        layer = ("char" if c.character_id is not None
                 else "songs" if c.kind == "songs" else "series")
        counter[layer] = counter.get(layer, 0) + 1
        layer_rank[c.chunk_id] = counter[layer]

    ranked = rr.rerank(q["question"], [c.text for c in merged], top_n=len(merged))
    order = []
    for idx, score in ranked:
        c = dataclasses.replace(merged[idx], score=score)
        order.append(c)

    out.update(order=order, layer_rank=layer_rank,
               pool_ids={c.chunk_id for c in pool},
               n_pinned=len(pinned), n_pool=len(merged))
    return out


def simulate(rec: dict, gold: set[int], *, quota_char: int, final: int,
             min_score: float, use_pin: bool) -> bool:
    """按给定参数从已打分的序列里筛出最终 chunk，判断是否命中标准答案。

    🚨 **`use_pin=False` 不等于「把 pinned 那几条删掉」** —— 这是我第一版写错的地方，
       10 题小样本上得到"直取关 = 0.0%"这个假结果。
       真实系统里关掉 ① 之后，角色本人的 chunk **仍可能被向量召回**；
       之所以在这里看不见，是因为合并时它被去重成了 pinned 那一份副本。
       ⇒ 正确的消融是「只丢掉**仅靠直取**才进来的那些」，
          留在池子里的按普通召回项处理（受配额与地板约束）。
       ⚠️ 与 I.8 ② 同一条教训：测试构造得不对，绿灯（或红灯）都毫无意义。
    """
    kept = []
    for c in rec["order"]:
        in_pool = c.chunk_id in rec["pool_ids"]
        if not use_pin and not in_pool:
            continue                          # 仅靠直取才有的，关掉后就没了
        as_pinned = c.pinned and use_pin
        if not as_pinned and in_pool:
            lr = rec["layer_rank"].get(c.chunk_id)
            if lr is not None and c.character_id is not None and lr > quota_char:
                continue                      # 模拟更窄的角色层配额
        kept.append(c if as_pinned else dataclasses.replace(c, pinned=False))
    picked = R._apply_pin_reserve(kept, final, min_score=min_score)
    return any(c.chunk_id in gold for c in picked)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    conn = db.connect()
    try:
        qs = sample_questions(conn, args.n, args.seed)
        print(f"抽样 {len(qs)} 道点名题（作品 fav_done ≥ {MIN_DONE:,}，种子 {args.seed}）\n")
        recs = []
        states: dict[str, int] = {}
        for i, q in enumerate(qs, 1):
            r = run_one(conn, q)
            key = r["state"] if r["scope_ok"] or r["state"] != "ok" else "ok_wrong_scope"
            states[key] = states.get(key, 0) + 1
            if "order" in r:
                recs.append((q, r))
            if i % 10 == 0 or i == len(qs):
                print(f"  {i}/{len(qs)}  可测 {len(recs)}")
    finally:
        conn.close()

    print("\n=== ① 解析（G.4 状态分布）===")
    for k, v in sorted(states.items(), key=lambda kv: -kv[1]):
        print(f"  {k:16} {v:4d}  {v / len(qs):6.1%}")
    print(f"⚠️ 只有解析正确的 {len(recs)} 道能测检索；其余是**解析层**的问题，不是检索层的。")
    if not recs:
        return 1

    def rate(**kw) -> float:
        return sum(simulate(r, q["gold"], **kw) for q, r in recs) / len(recs)

    cur = {"quota_char": R.QUOTA_CHAR, "final": R.FINAL,
           "min_score": R.MIN_SCORE, "use_pin": True}
    off = dict(cur, use_pin=False)
    print("\n=== ② 现行参数命中率 ===")
    print(f"  quota_char={R.QUOTA_CHAR} final={R.FINAL} MIN_SCORE={R.MIN_SCORE}")
    print(f"    直取=开（线上形态）  {rate(**cur):.1%}")
    print(f"    直取=关（消融）      {rate(**off):.1%}   "
          f"← 差值就是 ① alias 直取的净贡献（G.5g）")

    # ⚠️ **扫描要在「直取=关」下看才有判别力**：pinned 豁免地板、也不受配额约束
    #    （I.2 ① 那条保底席位的设计使然），所以直取开着时这三个参数几乎不影响
    #    点名题 —— 那本身是好事，但它让曲线是平的、测不出拐点。
    print("\n（以下两列：左 = 线上形态，右 = 关掉直取后**纯召回+rerank**的判别力）")

    def two(**kw) -> str:
        return f"{rate(**dict(cur, **kw)):.1%}   {rate(**dict(off, **kw)):.1%}"

    print("\n=== ③ MIN_SCORE 扫描（现值 0.05，此前由 10 条查询定）===")
    for ms in (0.0, 0.01, 0.05, 0.10, 0.20, 0.40):
        print(f"  {ms:<5}  {two(min_score=ms)}"
              f"{'   ← 现值' if ms == R.MIN_SCORE else ''}")

    print("\n=== ④ 角色层召回宽度扫描（现值 40）===")
    for qc in (5, 10, 20, 40, 50):
        print(f"  {qc:<5}  {two(quota_char=qc)}"
              f"{'   ← 现值' if qc == R.QUOTA_CHAR else ''}")

    print("\n=== ⑤ final 扫描（现值 8）===")
    for f in (3, 5, 8, 12):
        print(f"  {f:<5}  {two(final=f)}"
              f"{'   ← 现值' if f == R.FINAL else ''}")

    if args.json:
        args.json.write_text(json.dumps({
            "n_sampled": len(qs), "n_measurable": len(recs), "seed": args.seed,
            "min_done": MIN_DONE, "wide_recall": WIDE,
            "embed_fingerprint": embed.fingerprint(), **rr.descriptor(),
            "states": states,
            "current": {**cur, "hit": rate(**cur)},
            "no_pin": rate(**off),
            "sweep_min_score": {str(m): [rate(**dict(cur, min_score=m)),
                                         rate(**dict(off, min_score=m))]
                                for m in (0.0, 0.01, 0.05, 0.10, 0.20, 0.40)},
            "sweep_quota_char": {str(q): [rate(**dict(cur, quota_char=q)),
                                          rate(**dict(off, quota_char=q))]
                                 for q in (5, 10, 20, 40, 50)},
            "sweep_final": {str(f): [rate(**dict(cur, final=f)),
                                     rate(**dict(off, final=f))]
                            for f in (3, 5, 8, 12)},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写出 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

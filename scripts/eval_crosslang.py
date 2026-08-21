"""第 5 周评测 · B.3 跨语言惩罚复测（+ 翻译前后的真 A/B）。

原始测量（2026-08-16，语料转中文之前）：
    全库基准 39.2% 的作品简介是日文，而 8 条中文查询的 top-50 里只占 11.5%
    ⇒ **偏差 −27.7 个百分点**，日文简介作品的召回率不到应有的三分之一。

如今日文残留已降到 0.36% —— 本脚本回答两个问题：

  ① 现状还有没有惩罚？（直接复测原指标）
     ⚠️ 预期会遇到**统计功效问题**：日文作品只剩 39 部，400 次抽样的期望命中
        约 1.4 条，此时"偏差多少 pp"已经测不准。脚本会把功效算出来一起报，
        **不要拿一个没有功效的数字去宣称"惩罚消失了"**。

  ② **翻译前后的 A/B（本脚本的主产出）** —— 比 ① 强得多：
     译文缓存存着每条的日文原文（src），而 embed_cache 存着**翻译之前**
     那批日文文本的向量。把库里被翻译过的那些行的向量换回日文版，
     就得到一个"如果当初没翻译"的对照矩阵。
        同一批查询 · 同一个模型 · 同一批作品 —— **唯一的变量是语料语言。**
     这比"前后两次各测一个数"干净：那种做法混杂了查询集变化、模型抖动、
     库内容变化三个因素，而这里三者全部被钉死。

⚠️ **只读**：不写数据库、不写任何缓存。唯一的外部调用是给查询编码（约 10 次请求，
   成本可忽略）。

⚠️ **查询集写死在本文件里，这是有意的。** 原始那次测量的 8 条查询**没有被记录**
   （文档只提到其中三条），所以那个 −27.7pp 严格来说不可精确复现。
   从这次起查询集进版本库，后续任何复测都拿同一批。

用法：
    uv run --group etl python scripts/eval_crosslang.py
    uv run --group etl python scripts/eval_crosslang.py --top-k 50 --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from src import clients, db, embed, embed_cache, langclean, translate_cache

# ⚠️ **固定查询集 —— 改它就等于换了口径，改完必须重跑全部对照。**
# 选取原则：覆盖不同题材，且都是"描述性找番"（流程 B 路径①）的真实形态。
# 前三条来自原始测量的记录（文档里点名的那三条），其余为本次补齐。
QUERIES: tuple[str, ...] = (
    "主角很强但很低调的番",          # 原始测量记录在案的三条之一（当时 1/50）
    "科幻题材的动画",                # 同上（1/50）
    "悬疑推理",                      # 同上（1/50）
    "轻松搞笑的校园日常番",
    "关于组乐队的动画",
    "热血战斗番",
    "催泪的恋爱故事",
    "以美食为主题的动画",
    "机器人与人类共存的世界",
    "历史战国题材",
)


def load_profiles(conn) -> tuple[np.ndarray, list[str], np.ndarray]:
    """(ids, summaries, mat) —— 只取有向量的作品。

    ⚠️ halfvec 读回来是 HalfVector 对象、`to_numpy()` 给的是 float16，
       必须显式 astype(float32)（A.9 记过这个坑）。
    """
    with conn.cursor(binary=True) as cur:      # ⚠️ 文本格式会让向量列膨胀 6 倍
        cur.execute("""
            SELECT subject_id, COALESCE(summary, ''), vec
              FROM anime_profile
             WHERE vec IS NOT NULL
             ORDER BY subject_id
        """)
        rows = cur.fetchall()

    ids = np.array([r[0] for r in rows], dtype=np.int64)
    summaries = [r[1] for r in rows]
    mat = np.zeros((len(rows), embed.DIM), dtype=np.float32)
    for i, r in enumerate(rows):
        v = r[2]
        mat[i] = (v.to_numpy() if hasattr(v, "to_numpy") else np.asarray(v)
                  ).astype(np.float32)
    return ids, summaries, _l2(mat)


def _l2(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1.0, n)


def build_before_matrix(summaries: list[str], after: np.ndarray
                        ) -> tuple[np.ndarray, np.ndarray, int]:
    """把被翻译过的行换成**翻译之前**那份日文文本的向量。

    返回 (before_mat, translated_mask, 缓存未命中数)。
    未命中的行保持"现状向量"不变 —— 那等于把它算进对照组，是**保守**的方向：
    会让 A/B 的差异被低估，不会被夸大。
    """
    # 译文缓存里 dst=译文、src=日文原文。库里现在存的正是 dst。
    with translate_cache.connect() as tc:
        pairs = tc.execute("SELECT dst, src FROM translations").fetchall()
    dst_to_src = {d: s for d, s in pairs}

    want: dict[int, str] = {}          # 行号 → 翻译前的文本
    for i, s in enumerate(summaries):
        src = dst_to_src.get(s)
        if src is not None and src != s:
            want[i] = src

    with embed_cache.connect() as ec:
        vecs = embed_cache.get_many(ec, list(set(want.values())))

    before = after.copy()
    mask = np.zeros(len(summaries), dtype=bool)
    missed = 0
    for i, src in want.items():
        v = vecs.get(src)
        if v is None:
            missed += 1
            continue
        before[i] = v.astype(np.float32)
        mask[i] = True
    return _l2(before), mask, missed


def topk_share(mat: np.ndarray, qvecs: np.ndarray, mask: np.ndarray,
               k: int) -> tuple[float, list[int]]:
    """目标集合（mask）在各查询 top-k 里的占比，以及逐查询命中数。"""
    hits: list[int] = []
    for q in qvecs:
        idx = np.argpartition(-(mat @ q), k)[:k]
        hits.append(int(mask[idx].sum()))
    return sum(hits) / (k * len(qvecs)), hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-k", type=int, default=50)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    k = args.top_k

    conn = db.connect()
    try:
        ids, summaries, after = load_profiles(conn)
    finally:
        conn.close()                    # ⚠️ 后面是长耗时的 API 阶段，别握着连接

    print(f"作品（有向量）  {len(ids):,}")

    # ── ① 现状：直接复测原指标 ──────────────────────────────────
    jp_now = np.array([langclean.is_japanese(s) for s in summaries])
    base_now = jp_now.mean()

    print("\n编码查询…")
    qvecs = _l2(np.vstack([embed.embed_query(q) for q in QUERIES]))

    share_now, hits_now = topk_share(after, qvecs, jp_now, k)
    n_draw = k * len(QUERIES)
    expected = base_now * n_draw

    print("\n=== ① 现状（语料已转中文）===")
    print(f"全库日文简介基准率        {base_now:7.2%}  ({jp_now.sum()} / {len(ids)})")
    print(f"{len(QUERIES)} 条查询 top-{k} 里的占比   {share_now:7.2%}  "
          f"({sum(hits_now)} / {n_draw})")
    print(f"偏差                      {(share_now - base_now) * 100:+7.2f} pp"
          f"     （原始测量：−27.7 pp）")
    print(f"⚠️ 统计功效：零假设下期望命中 {expected:.1f} 条 —— "
          f"{'样本量不足，这个偏差没有判别力' if expected < 5 else '可用'}")

    # ── ② 翻译前后的 A/B ────────────────────────────────────────
    before, tmask, missed = build_before_matrix(summaries, after)
    n_tr = int(tmask.sum())
    print(f"\n=== ② A/B：把 {n_tr:,} 部作品的向量换回翻译前的日文版 ===")
    if missed:
        print(f"⚠️ 缓存未命中 {missed} 条，这些行保持现状向量（保守方向：低估差异）")
    if n_tr == 0:
        print("🚨 一条都没换成 —— 缓存里没有翻译前的向量，A/B 做不了")
        return 1

    base_tr = tmask.mean()
    share_before, hits_before = topk_share(before, qvecs, tmask, k)
    share_after, hits_after = topk_share(after, qvecs, tmask, k)

    print(f"\n对照的是**同一批 {n_tr:,} 部作品**，占全库 {base_tr:.2%}"
          f"（= 零惩罚时它们该有的占比）\n")
    print(f"{'':22} {'top-' + str(k) + ' 占比':>12} {'相对基准':>10}")
    print(f"{'翻译前（日文简介）':22} {share_before:11.2%} "
          f"{(share_before - base_tr) * 100:+9.2f} pp")
    print(f"{'翻译后（中文简介）':22} {share_after:11.2%} "
          f"{(share_after - base_tr) * 100:+9.2f} pp")
    print(f"{'净改善':22} {(share_after - share_before) * 100:+11.2f} pp")

    print(f"\n逐查询命中数（满分 {k}，基准期望 {base_tr * k:.1f}）：")
    print(f"  {'查询':26} {'前':>5} {'后':>5}")
    for q, hb, ha in zip(QUERIES, hits_before, hits_after, strict=True):
        print(f"  {q:26} {hb:5d} {ha:5d}")

    if args.json:
        args.json.write_text(json.dumps({
            "queries": list(QUERIES), "top_k": k,
            "embed_fingerprint": embed.fingerprint(),
            "now": {"baseline": base_now, "share": share_now,
                    "hits": hits_now, "expected_under_null": expected},
            "ab": {"n_translated": n_tr, "cache_missed": missed,
                   "baseline": base_tr,
                   "share_before": share_before, "share_after": share_after,
                   "hits_before": hits_before, "hits_after": hits_after},
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

"""探测硅基流动 embedding API 的行为 —— 建库脚本的前置。

第 3 周动作 1（见 CLAUDE.md A.7）。**在写 build_embeddings.py 之前跑一次。**

⚠️ 这不是「本地 vs API 一致性验证」—— 那个方案已废弃（A.7）。
   现在是全程走 API，所以要摸清的是**唯一那条路径自己的行为**：
   它是不是一个确定的、可复现的、我们控得住的函数。

跑法：
    uv run --group etl python scripts/probe_embedding_api.py

成本：约 1.5 万 token ≈ ¥0.001。

五项检查对应的下游决策：

  1. 维度与归一化 → sql/003 的 halfvec(1024) 对不对；
                    以及 pgvector 能不能用 `<#>` 内积算子
  2. 确定性       → 缓存层「bit-identical 重放」的承诺成不成立
  3. 批内不变性   → 同上。这一项挂了的话缓存只能保证「近似」重放
  4. instruct 前缀 → query/document 非对称编码控不控得住
  5. dimensions   → plot_chunk 的 512 维用服务端截断还是客户端截断
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ⚠️ `python scripts/xxx.py` 只把 scripts/ 放进 sys.path，项目根不在里面，
#    `from src import db` 会 ModuleNotFoundError。scripts/ 下每个脚本都有这一行。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import numpy as np
from dotenv import load_dotenv

from src import db

BASE_URL = "https://api.siliconflow.cn/v1/embeddings"

# ⚠️ **模型名锁死在代码里，不放 .env。**
#    embedding 换模型 = 向量空间换基 = 库里所有向量作废（CLAUDE.md A.8）。
#    做成环境变量等于给「静默换模型」开了个口子，而这类故障不报错、
#    只是检索结果变成噪声。探测脚本允许 --model 覆盖是为了试错模型名；
#    建库脚本里这个常量不接受覆盖。
MODEL = "Qwen/Qwen3-Embedding-0.6B"

# Qwen3 是指令条件化的：query 侧加前缀、document 侧不加。
# 这个前缀的具体措辞会影响向量，所以它一旦定下来也必须锁死。
QUERY_INSTRUCT = "Instruct: 根据描述检索相似的动画作品\nQuery: "

TIMEOUT = 60.0


def embed(
    texts: list[str],
    api_key: str,
    *,
    model: str = MODEL,
    dimensions: int | None = None,
) -> np.ndarray:
    """调一次 /v1/embeddings，返回 (n, dim) 的 float32 数组。

    ⚠️ 返回 float32 而不是 float16 —— 缓存层要存 API 的原值（A.9），
       降精度是写库那一步的事，不是这里。
    """
    payload: dict = {"model": model, "input": texts}
    if dimensions is not None:
        payload["dimensions"] = dimensions

    r = httpx.post(
        BASE_URL,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")

    data = r.json()["data"]
    # ⚠️ 不能假设返回顺序与输入一致，按 index 重排。
    data.sort(key=lambda d: d["index"])
    return np.array([d["embedding"] for d in data], dtype=np.float32)


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


def sample_texts() -> tuple[list[tuple[int, str, str]], list[tuple[int, str, str]]]:
    """从库里取真实简介：一对同系列的 + 一对无关的。

    用真实数据而不是造句，因为要检查的正是「模型在本项目的中文语料上
    表现是否正常」。同系列那一对提供了一个弱 ground truth：
    它们的余弦应当明显高于无关的那一对。

    ⚠️ **「无关」那一对不能按 subject_id 顺序取。** 实测相邻 id 往往是同一系列
       （`LIMIT 2 OFFSET 5000` 取到的是「奶油柠檬 第十三部分 / 第十四部分」），
       那样检查 6 会拿两部同系列作品当反例，必然假红。
       改用已有的 `tag_vec` 挑：在够热门、简介够长的作品里，找与基准作品
       **tag 余弦最低**的那一部 —— 这是个有依据的「无关」，不是碰运气。

    ⚠️ 全部查询都是确定性的（无 random()），多次运行取到同一批，便于复现。
    """
    # 简介太短的样本没有代表性（实测有 25 字的），统一要求 ≥150 字
    MIN_LEN = 150

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            WITH multi AS (
                SELECT series_root
                  FROM anime_profile
                 WHERE series_root IS NOT NULL
                   AND char_length(coalesce(summary, '')) >= %(min_len)s
                 GROUP BY series_root HAVING count(*) >= 2
                 ORDER BY series_root
                 LIMIT 1
            )
            SELECT subject_id, name_cn, summary
              FROM anime_profile
             WHERE series_root = (SELECT series_root FROM multi)
               AND char_length(coalesce(summary, '')) >= %(min_len)s
             ORDER BY subject_id
             LIMIT 2
        """, {"min_len": MIN_LEN})
        same_series = cur.fetchall()

        # 基准作品 = 库里最热门且简介够长的一部
        cur.execute("""
            SELECT subject_id, name_cn, summary
              FROM anime_profile
             WHERE char_length(coalesce(summary, '')) >= %(min_len)s
               AND tag_vec IS NOT NULL
             ORDER BY fav_done DESC
             LIMIT 1
        """, {"min_len": MIN_LEN})
        anchor = cur.fetchone()

        # 与基准 tag 余弦最低的一部（`<=>` 是余弦距离，DESC = 最不相似）
        cur.execute("""
            WITH a AS (
                SELECT tag_vec, coalesce(series_root, subject_id) AS root
                  FROM anime_profile WHERE subject_id = %(anchor)s
            )
            SELECT b.subject_id, b.name_cn, b.summary
              FROM anime_profile b, a
             WHERE b.tag_vec IS NOT NULL
               AND char_length(coalesce(b.summary, '')) >= %(min_len)s
               AND b.fav_done >= 500          -- 排掉冷门条目，tag 噪声大
               AND coalesce(b.series_root, b.subject_id) <> a.root
             ORDER BY b.tag_vec <=> a.tag_vec DESC
             LIMIT 1
        """, {"anchor": anchor[0], "min_len": MIN_LEN})
        far = cur.fetchone()

    return same_series, [anchor, far]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=MODEL, help=f"覆盖模型名（默认 {MODEL}）")
    args = ap.parse_args()

    load_dotenv()
    api_key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not api_key:
        print("✗ 缺少环境变量 SILICONFLOW_API_KEY（见 .env.example）", file=sys.stderr)
        return 1

    print(f"模型：{args.model}")
    print(f"端点：{BASE_URL}\n")

    same_series, unrelated = sample_texts()
    if len(same_series) < 2 or len(unrelated) < 2:
        print("✗ 库里样本不足，先跑 load_profiles.py / build_tag_vectors.py", file=sys.stderr)
        return 1

    for label, rows in (("同系列", same_series), ("无关", unrelated)):
        for sid, name, summary in rows:
            print(f"  [{label}] {sid} {name}：{summary[:40]}…")
    print()

    texts = [r[2] for r in same_series] + [r[2] for r in unrelated]
    probe = texts[0]

    verdicts: list[tuple[str, bool | None, str]] = []

    # ── 1. 维度与归一化 ────────────────────────────────────────────
    try:
        v = embed([probe], api_key, model=args.model)
    except RuntimeError as e:
        print(f"✗ 首次请求失败：{e}", file=sys.stderr)
        print("\n提示：401 → key 不对；400/404 → 模型名不对，试试 --model 换个写法",
              file=sys.stderr)
        return 1

    dim = v.shape[1]
    norm = float(np.linalg.norm(v[0]))
    normalized = abs(norm - 1.0) < 1e-3
    print(f"1. 维度 = {dim}    L2 范数 = {norm:.6f}")
    verdicts.append((
        "维度 1024",
        dim == 1024,
        f"实际 {dim}。sql/003 建的是 halfvec(1024)，不符要改" if dim != 1024 else "与 sql/003 一致",
    ))
    verdicts.append((
        "返回已归一化",
        normalized,
        "可以用 pgvector 的 <#> 内积算子（比 <=> 略快）" if normalized
        else "⚠️ 未归一化：必须自己归一化后再入库，否则偏好向量的加权求和会被范数扭曲",
    ))

    # ── 2. 跨请求确定性：同一文本、同样的单条请求，发两次 ──────────
    # ⚠️ **不要期待逐位相同。** 服务端是连续批处理（continuous batching）：
    #    你的请求会和**其他用户同时在飞的请求**拼成一个 batch，
    #    padding 长度和规约顺序因此完全不受你控制 —— 连自己的 batch size
    #    定死也没用。实测同一条文本两次请求逐位差在 0 ~ 2e-3 之间跳。
    #    所以这一项和第 3 项测的是同一个现象，判据同样只能看余弦。
    v2 = embed([probe], api_key, model=args.model)
    d_det = float(np.abs(v[0] - v2[0]).max())
    c_det = cos(v[0], v2[0])
    print(f"2. 跨请求确定性 cos = {c_det:.8f}   （最大逐位差 = {d_det:.3e}）")
    # ⚠️ 判据推迟到第 6 项之后 —— 噪声的绝对值没有意义，
    #    要和这批语料里的**信号强度**比才知道大小。见文件末尾的信噪比判据。

    # ── 3. 批内不变性：同一文本放进不同组成的 batch ────────────────
    # A：probe 在首位，同伴是同系列的另一部
    # B：probe 在末位，同伴是两部无关作品 —— 位置、邻居、batch 大小都变了
    #
    # ⚠️ **判据是余弦不是逐位差。** batch 里文本长度不同 → padding 长度不同
    #    → GPU 走不同的 kernel/规约顺序，浮点结果必然有差异。这是批量推理的
    #    固有行为，不是 API 的 bug，逐位相同本来就不该期待。
    #    真正要回答的是「这点差异会不会改变检索结果」，那只取决于余弦。
    #
    # ⚠️ 而且这**不影响缓存的可复现性** —— 缓存存的就是 API 返回的那个向量，
    #    从缓存重放永远精确。batch 组成不进 cache key。
    #    受影响的只有「缓存未命中、重新请求」的场景（如第 6 周季度同步新增作品）。
    va = embed([probe, texts[1]], api_key, model=args.model)[0]
    vb = embed([texts[2], texts[3], probe], api_key, model=args.model)[2]
    d_batch = float(np.abs(va - vb).max())
    c_batch = cos(va, vb)
    print(f"3. 批内不变性   cos = {c_batch:.8f}   （最大逐位差 = {d_batch:.3e}）")

    # 排序稳定性：真正会影响检索的是「顺序变没变」，不是「数值差多少」。
    # 拿两个版本的 probe 各自去和其余样本比相似度，看排名是否一致。
    others = embed(texts[1:], api_key, model=args.model)
    rank_a = np.argsort(-(others @ va))
    rank_b = np.argsort(-(others @ vb))
    rank_same = bool((rank_a == rank_b).all())
    print(f"   排序稳定性   {'一致' if rank_same else '⚠️ 不一致'}"
          f"（{len(texts) - 1} 个样本的相似度排名）")

    # 判据同样推迟到信噪比那一步。

    # ── 4. instruct 前缀敏感性 ─────────────────────────────────────
    v_plain = embed([probe], api_key, model=args.model)[0]
    v_instr = embed([QUERY_INSTRUCT + probe], api_key, model=args.model)[0]
    c_prefix = cos(v_plain, v_instr)
    print(f"4. 前缀敏感性   cos(无前缀, 加前缀) = {c_prefix:.6f}")
    verdicts.append((
        "前缀在我们手里",
        c_prefix < 0.999,
        "加前缀确实改变向量 → query/doc 非对称编码可控" if c_prefix < 0.999
        else "⚠️ 前缀几乎不影响输出：要么服务端做了归一化处理，"
             "要么它自己就在加前缀。非对称编码可能不受我们控制",
    ))

    # ── 5. dimensions 参数 + 服务端截断 vs 客户端截断 ──────────────
    try:
        v512 = embed([probe], api_key, model=args.model, dimensions=512)[0]
    except RuntimeError as e:
        print(f"5. dimensions   不支持（{str(e)[:80]}）")
        verdicts.append((
            "dimensions 参数",
            None,
            "不支持 → plot_chunk 的 512 维走客户端截断（MRL 截断合法，无损失）",
        ))
    else:
        # MRL 的客户端截断：取前 N 维再 L2 归一化
        trunc = v_plain[:512]
        trunc = trunc / np.linalg.norm(trunc)
        server = v512 / np.linalg.norm(v512)
        c_trunc = cos(trunc, server)
        print(f"5. dimensions   支持，dim={v512.shape[0]}；"
              f"cos(客户端截断, 服务端截断) = {c_trunc:.6f}")
        verdicts.append((
            "两种截断等价",
            c_trunc > 0.999,
            "两种都行，优先客户端（缓存存 1024，随时可再切维度）" if c_trunc > 0.999
            else "⚠️ 两者不等价：服务端截断可能不是纯 MRL。"
                 "⚠️ 缓存存 1024 + 客户端截断更安全，至少口径自洽",
        ))

    # ── 6. 语义合理性（弱 ground truth）────────────────────────────
    allv = embed(texts, api_key, model=args.model)
    c_same = cos(allv[0], allv[1])
    c_diff = cos(allv[2], allv[3])
    print(f"6. 语义合理性   同系列 cos = {c_same:.4f}   无关 cos = {c_diff:.4f}")
    verdicts.append((
        "同系列 > 无关",
        c_same > c_diff,
        "模型在本项目的中文语料上工作正常" if c_same > c_diff
        else "⚠️ 同系列反而更不像 —— 模型名或语料可能有问题，别急着建库",
    ))

    # ── 7. 信噪比：噪声的绝对值没意义，要和信号比 ──────────────────
    # ⚠️ 检查 2、3 测的是**同一个现象**：服务端连续批处理带来的浮点不确定性。
    #    与其对余弦拍一个「0.9999」之类的常数阈值（没有依据），
    #    不如拿它和这批语料里真实的信号强度比 —— 后者是自校准的。
    signal = c_same - c_diff                      # 「同系列 vs 无关」的区分度
    noise = max(1.0 - c_det, 1.0 - c_batch)       # 重发请求带来的漂移
    snr = signal / noise if noise > 0 else float("inf")
    print(f"\n7. 信噪比       信号(同系列−无关) = {signal:.4f}   "
          f"噪声(重发漂移) = {noise:.2e}   →  {snr:.0f}×")

    verdicts.append((
        "噪声远小于信号",
        snr > 100,
        f"信号是噪声的 {snr:.0f} 倍 → 对检索质量无实质影响" if snr > 100
        else f"⚠️ 信噪比只有 {snr:.0f} 倍 —— 重发请求会实质改变检索结果",
    ))
    verdicts.append((
        "排序稳定",
        rank_same,
        "小样本上排序未变（⚠️ 只有几个样本，检出力弱，不能当强证据）" if rank_same
        else "⚠️ 连小样本的排序都变了，问题比信噪比显示的更严重",
    ))
    verdicts.append((
        # None = 已知结论，不算失败项 —— 它不是要修的问题，是要接受并绕开的事实
        "bit-identical 可复现",
        None,
        (
            "⚠️ **做不到，且无法通过控制分批规避** —— 服务端连续批处理会把你的"
            "请求和其他用户在飞的请求拼一起。⇒ **可复现性只能靠缓存层**（A.7），"
            "这不再是「稳妥起见」而是实测结论"
        ),
    ))

    # ── 汇总 ───────────────────────────────────────────────────────
    print("\n" + "─" * 64)
    failed = 0
    for name, ok, note in verdicts:
        mark = "•" if ok is None else ("✓" if ok else "✗")
        if ok is False:
            failed += 1
        print(f"  {mark} {name:<16} {note}")
    print("─" * 64)

    if failed:
        print(f"\n{failed} 项不符预期 —— 先看上面的 ⚠️，别直接跑建库脚本。")
    else:
        print("\n全部符合预期，可以往下写 build_embeddings.py。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

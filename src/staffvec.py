"""staff / studio 向量的**唯一**计算实现。

⚠️ 与 src/tagvec.py 同一条纪律：向量只在这里算一次，结果写进
   `anime_profile.staff_vec`，线上 SQL 打分与第 5 周评测的 numpy 打分都读那一列。
   两条路径因此不可能出现口径差异 —— 构造上的保证，不是靠人记得同步。

⚠️ idf 依赖全库词频，向量**只能全量重算，不能逐行增量更新**。
   第 6 周季度同步加入新作品后，必须整列重跑 scripts/build_staff_vectors.py。

---

**特征是 `(角色, 人名)` 二元组，不是光人名。** 同一个人做导演和做音乐是两种
不同的口味信号。dump 里的角色只有 5 种，都是干净的结构化数据：
脚本 15,263 · 导演 10,121 · 人物设定 10,043 · 原作 9,496 · 音乐 8,940。

**加权是 binary × idf 再 L2 归一化** —— 与 tag_vec 的 `log(1+count) × idf` 不同，
因为 staff 没有 count 可用（一部作品要么有这个人要么没有）。

⚠️ **idf 在这里不是可选项。** 实测 df 最高的「导演」是 `雷火剣`（143 部），
   那是里番常用化名 —— 没有 idf 它会变成一个强噪声维度。

**阈值 df>=8 → 1,933 维。** 实测降低阈值只增加词表不增加覆盖：
两两非零重叠率在 df>=2 时是 6.95%、df>=8 时是 6.91% —— 那些罕见特征本就很少共现。
df>=2 要 7,234 维（多 5,300 维）却只换来 0.04% 覆盖，不值。

⚠️ **这是一个「加成信号」不是「排序信号」。** 实测真实场景（用户评 10 部热门番）
   全库只有 **16.6%** 的作品拿到非零 staff 分数 —— 93% 的作品对之间根本没有
   共同人员。所以融合权重 γ 应当偏小，它的作用形态是「命中就加分」
   而非「参与全局排序」。⬜ 具体值第 5 周评测决定。

   发声时精度很高：冰菓 → 冰菓OVA(0.86) · Free! Starting Days(0.71) ·
   小凉宫春日(0.70) · 凉宫春日2009(0.58)，全是京阿尼。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path

VOCAB_PATH = Path(__file__).resolve().parent.parent / "data" / "interim" / "staff_vocab.json"

# 词表阈值。改它必须重跑 scripts/build_staff_vectors.py 并重新提交词表文件。
MIN_DF = 8

# 列宽，必须与 sql/006_staff_vec.sql 的 sparsevec(1933) 一致。
# ⚠️ 词表实际大小若因数据更新而变化，build_staff_vectors.py 会拒绝写入而不是
#    悄悄截断 —— 维度对不上是「不报错但全错」的典型场景。
DIM = 1933


def features_of(studios: list[str] | None, staff: list[dict] | None) -> set[str]:
    """一部作品的特征集合。

    ⚠️ 返回 set：同一部作品里同一个 (角色, 人名) 出现两次不该加倍权重
       （dump 里确实有重复条目）。
    """
    out: set[str] = set()
    for s in studios or []:
        if s:
            out.add(f"S\x1f{s}")
    for p in staff or []:
        role, name = (p or {}).get("role"), (p or {}).get("name")
        if role and name:
            out.add(f"P\x1f{role}\x1f{name}")
    return out


def build_vocab(rows: list[tuple]) -> tuple[list[str], dict[str, float]]:
    """rows = [(subject_id, studios, staff), ...] → (词表, {特征: idf})。

    词表**按字典序排列**，与 tagvec.vocab() 同一条理由：
    顺序固定，否则库里的向量与内存里的对不上号。
    """
    df: Counter[str] = Counter()
    for _sid, studios, staff in rows:
        df.update(features_of(studios, staff))

    vocab = sorted(f for f, c in df.items() if c >= MIN_DF)
    n = len(rows)
    idf = {f: math.log(n / df[f]) for f in vocab}
    return vocab, idf


def weights_of(studios: list[str] | None, staff: list[dict] | None,
               index: dict[str, int], idf: dict[str, float]) -> dict[int, float]:
    """一部作品的 {维度下标: 权重}，已 L2 归一化。

    空 dict 表示该作品没有任何在词表内的特征 —— 调用方应当存 NULL 而非零向量。
    """
    raw = {index[f]: idf[f] for f in features_of(studios, staff) if f in index}
    if not raw:
        return {}
    norm = math.sqrt(sum(v * v for v in raw.values()))
    return {k: v / norm for k, v in raw.items()}


def to_sparsevec(weights: dict[int, float], dim: int = DIM) -> str:
    """{下标: 权重} → pgvector 的 sparsevec 字面量 `{i:v,...}/dim`。

    ⚠️ **下标从 1 开始**（pgvector 的 sparsevec 是 1-based），而 Python 的
       词表下标从 0 开始。差一位不会报错，只会让每一维的含义整体错位 ——
       这正是「不报错但全错」那一类，所以转换只在这一个函数里做。
    """
    if not weights:
        raise ValueError("空权重应当存 NULL，不要转成零向量")
    body = ",".join(f"{i + 1}:{v:.7g}" for i, v in sorted(weights.items()))
    return "{" + body + "}/" + str(dim)


def fingerprint(vocab: list[str]) -> str:
    """词表指纹，写进 build_meta 并在读向量前比对。

    ⚠️ 词表一变，库里所有 staff_vec 立刻失效**且不报错** —— 维度还是 1933、
       余弦照算，只是每一维代表谁全错了。这个指纹是唯一的防线。
    """
    payload = json.dumps({"min_df": MIN_DF, "dim": DIM, "vocab": vocab},
                         ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def save_vocab(vocab: list[str], idf: dict[str, float]) -> None:
    """词表入 git。与 tag_vocab.json 同一条理由：它不是纯派生数据，
    而是「库里这批向量的每一维代表谁」的唯一记录，换机器必须能复现。
    """
    VOCAB_PATH.parent.mkdir(parents=True, exist_ok=True)
    VOCAB_PATH.write_text(json.dumps({
        "min_df": MIN_DF, "dim": DIM, "fingerprint": fingerprint(vocab),
        "vocab": vocab, "idf": {f: round(idf[f], 6) for f in vocab},
    }, ensure_ascii=False, indent=1), encoding="utf-8")


def load_vocab() -> tuple[list[str], dict[str, float]]:
    """读回词表。线上解释「为什么推荐它」时要用（命中了哪个公司/监督）。"""
    d = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    return d["vocab"], d["idf"]

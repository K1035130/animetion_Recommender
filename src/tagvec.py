"""tag 向量的**唯一**计算实现。

⚠️ 这个模块存在的理由和 textproc 一样，是纪律：
    向量只在这里算一次，结果写进 anime_profile.tag_vec，
    线上 SQL 打分与第 5 周评测的 numpy 批量打分都读那一列。
    两条路径因此不可能出现口径差异 —— 这是构造上的保证，不是靠人记得同步。

    在 2026-08-12 之前，向量是 recommend.build_catalog() 每次启动重算的。
    改用 Vercel serverless（无常驻进程）后线上打分必须走 SQL，
    若让 SQL 读库里的向量、评测重算一份，就会退化成两套口径 ——
    而第 5 周评测正是这个项目的核心卖点，口径不一致等于结论不可信。

⚠️ idf 依赖全库词频，所以向量**只能全量重算，不能逐行增量更新**。
   第 6 周季度同步加入新作品后，必须整列重跑 scripts/build_tag_vectors.py。
"""

from typing import Literal

import numpy as np

from src import tag_rules
from src.textproc import keep_tags

Weighting = Literal["logtf-idf", "binary"]


def vocab() -> list[str]:
    """向量的维度顺序。排序固定，否则库里的向量与内存里的对不上号。"""
    return sorted(keep_tags())


def _clean(names, counts, vocab_set: frozenset[str]) -> dict[str, float]:
    """把一组原始 tag 名归一化、分流、并到词表维度上。"""
    out: dict[str, float] = {}
    for nm, ct in zip(names, counts, strict=True):
        canon = tag_rules.normalize(nm)
        if canon not in vocab_set or tag_rules.classify(canon) != "KEEP":
            continue
        out[canon] = out.get(canon, 0.0) + float(ct)
    return out


def weights_of(tags: list[dict] | None, meta_tags: list[str] | None,
               vocab_set: frozenset[str]) -> dict[str, float]:
    """一部作品的 {tag: 原始权重}。用户 tag 与官方 meta_tags 落进同一个空间。

    meta_tags 没有票数，用该作品 tag 票数的中位数 —— 官方标签的可信度不低于
    用户 tag，但也不该压过最高票的那个。作品一个 tag 都没有时退化为 1，
    此时向量各维等权，L2 归一化后依然可用。

    兜底对「清洗后 tags 为空」的作品是必需的：化物语(done=37,573) 的 11 个
    用户 tag 全是 staff/CV/年份，只有 meta_tags 的「奇幻」「小说改」能给它向量。
    ⚠️ 但效果有限 —— tags 为空的 166 部里只有 24 部能靠 meta_tags 拿到非零向量。
    """
    tags = tags or []
    w = _clean([t["name"] for t in tags], [t.get("count") or 0 for t in tags],
               vocab_set)
    base = float(np.median(list(w.values()))) if w else 1.0
    metas = meta_tags or []
    for name, val in _clean(metas, [base] * len(metas), vocab_set).items():
        w.setdefault(name, val)
    return w


def compute(rows: list[tuple], weighting: Weighting = "logtf-idf"
            ) -> tuple[list[str], np.ndarray]:
    """rows = [(subject_id, tags, meta_tags), ...] → (vocab, (n, 308) float32)

    权重：log(1+count) × idf，再按行 L2 归一化。
      · log —— count 随作品热度缩放（done=50 的作品 tag count 是个位数，
        done=50,000 的是几千），不压缩量级的话热门作品会主导一切
      · idf —— 「漫画改」覆盖 3,798 部，几乎不携带口味信息，应当降权
      · L2 —— 余弦的前提；同时抵消「tag 多的作品模长更大」

    binary 模式留给第 5 周做 ablation（第 10 节的可选对照）。

    ⚠️ 行顺序即 rows 的顺序，调用方负责保证它与 subject_id 对齐。
    """
    vocab_set = keep_tags()
    vlist = vocab()
    col = {t: i for i, t in enumerate(vlist)}

    mat = np.zeros((len(rows), len(vlist)), dtype=np.float32)
    for i, (_sid, tags, metas) in enumerate(rows):
        for t, c in weights_of(tags, metas, vocab_set).items():
            mat[i, col[t]] = 1.0 if weighting == "binary" else np.log1p(c)

    if weighting == "logtf-idf":
        dfreq = (mat > 0).sum(axis=0)
        idf = np.log((len(rows) + 1) / (dfreq + 1)).astype(np.float32)
        mat *= idf

    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0        # 零向量保持为零，不产生 nan
    mat /= norm
    return vlist, mat

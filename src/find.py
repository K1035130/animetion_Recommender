"""流程 B · 找番（G.1 路径①，2026-08-24 实现）—— 用一段描述找作品。

与流程 C（剧情问答）的区别：流程 C 先要**确认是哪部作品**才能检索；
这里反过来，**不知道是哪部**正是触发条件——用户拿不出片名，只有一段描述
（「主角很强但很低调的番」），检索目标是 `anime_profile.vec`（简介向量，
G.1 路径①），返回作品列表而不是 chunk。

⚠️ **只用语义腿，不混 BM25。** `scripts/eval_find.py` 实测过
`RRF(semantic, bm25)`：在**转述类查询**（找番的主要形态——想得起标签名的
用户直接在问卷/标签页筛选就行，会来"找番"的恰恰是想不起怎么描述的那批）上，
NDCG@10 从纯语义的 0.576 掉到 RRF 的 0.448，**净负收益**；字面命中 tag 的
查询上 RRF 也只是打平（0.772→0.812，n=6，噪声量级）。BM25 腿的噪声比信号大，
拖累的是语义腿本来就答对的那部分。⇒ 与 HyDE 的结论同一个形状
（G.5c：多花一次确定的成本，换统计上为零甚至为负的收益）：**不做**。
留着 `scripts/eval_find.py` 作为这个负结果的复测入口，不进 `src/`。

⚠️ **复用 `recommend_sql.score()` 而不是另写一套 SQL**：推荐侧的续作折叠
（同系列只留入口作品）与 nsfw 过滤对"找番"同样成立——用户想要的是
"这类的番有哪些"，不是同一部剧场版/OVA/重制版轮着刷屏。传
`weights=(tag=0, emb=1, staff=0)` 只点亮语义腿，`prefs` 的 tag/staff 位置
填零向量（`_active()` 会因为权重是 0 而跳过它们，零向量只是占位，不参与打分）。

⚠️ **不设 `MIN_SCORE` 评分下限**（`min_score=None`）。这是发现型检索不是
个性化推荐：候选池本身已经是 `done>=50` 的 11,453 部（候选集口径，见
`src/candidates.py`），再加评分下限是"推荐"的产品语义，"找番"要的是
语义匹配优先——一部小众但完全对味的作品，不该因为评分人数不够被排除。

⚠️ **不涉及剧透门控**：返回的是作品名 + 年份，没有剧情文本，G.2 那条
"找番路径无条件屏蔽剧透"针对的是路径③跨作品 chunk 检索，这里不适用。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import psycopg

from src import embed
from src import recommend_sql as R

TOP_K_DEFAULT = 8

# recommend_sql._SPACES 的三路维度：tag(308) / emb(1024) / staff(1933)。
# ⚠️ 这三个数字与 sql/002_tag_vec.sql / embed.DIM / sql/006_staff_vec.sql
#    的列宽是同一份事实，这里只是占位（权重 0，_active() 不会真的用它们算余弦），
#    维度错了也不会算错，但保持一致，免得将来有人拿它当"真实向量"复制走。
_ZERO_TAG = np.zeros(308, dtype=np.float32)
_ZERO_STAFF = np.zeros(1933, dtype=np.float32)
_WEIGHTS = R.Weights(tag=0.0, emb=1.0, staff=0.0)


@dataclass(frozen=True)
class FindHit:
    subject_id: int
    name: str
    air_year: int | None
    # 语义腿余弦，[-1,1]。⚠️ 与 /recommend 的 match 同一条纪律：
    # 量纲不跨请求可比，只用于同一次结果内部排序，别展示成百分比。
    match: float


def find(conn: psycopg.Connection, query: str, *,
         top_k: int = TOP_K_DEFAULT,
         include_nsfw: bool = False,
         retries: int | None = None,
         timeout: float | None = None) -> list[FindHit]:
    """用自然语言描述找作品。零结果时返回空列表（不是异常）。

    两次往返：编码 + `recommend_sql.score()`（含折叠与过滤）一次，
    补 `air_year` 一次（`Recommendation` 不带这一列，模式与
    `recommend_sql.explain()` 补 tag 理由同构：只查最终返回的几行）。

    ⚠️ `retries`/`timeout` 透传给 `embed.embed_query()`——**请求路径必须传
    短预算**，模块级默认值是给离线批量任务标定的，直接用会在服务端卡顿时
    卡住整个请求（I.4 实测放大到 883 秒的教训）。调用方应传
    `src.retrieve.REQUEST_EMBED_RETRIES` / `REQUEST_EMBED_TIMEOUT` 那一套。
    """
    qvec = embed.embed_query(query, retries=retries, timeout=timeout)
    recs = R.score(conn, [], prefs=(_ZERO_TAG, qvec.astype(np.float32), _ZERO_STAFF),
                   weights=_WEIGHTS, rank_by="match", min_score=None,
                   include_nsfw=include_nsfw, top_k=top_k)
    if not recs:
        return []

    with conn.cursor() as cur:
        cur.execute("SELECT subject_id, air_year FROM anime_profile "
                    "WHERE subject_id = ANY(%s)",
                    ([r.subject_id for r in recs],))
        years = dict(cur.fetchall())

    return [FindHit(subject_id=r.subject_id, name=r.name,
                    air_year=years.get(r.subject_id), match=r.match)
            for r in recs]

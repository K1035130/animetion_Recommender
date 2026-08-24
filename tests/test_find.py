"""src/find.py 的 find() 单测 —— 流程 B 找番的核心函数。

⚠️ 同一条纪律：**不打真实 embedding API**。用 monkeypatch 把
`embed.embed_query` 换成一个可控的返回值，测的是"编码结果被怎么用"，
不是"编码准不准"（那是 A.7 已经实测过的事，不需要在这里重复验证）。

一个技巧：**拿库里某部作品自己的 vec 当"假想查询向量"**，而不是随便造一个
随机向量——这样断言"最像的那条就是它自己"是有真实语义支撑的余弦为 1.0，
不是纯粹对着 mock 数据自娱自乐的管道测试。
"""

import numpy as np
import pytest

from src import db, embed
from src import find as find_mod
from src import recommend_sql as R

# 孤独摇滚！——test_api.py 的 test_anime_detail 已经在用这个 id，
# 已知非 nsfw、有完整 vec，复用同一个已验证过的样本。
BOCCHI = 328609


@pytest.fixture(scope="module")
def conn():
    with db.connect() as c:
        yield c


@pytest.fixture(scope="module")
def bocchi_vec(conn) -> np.ndarray:
    with conn.cursor() as cur:
        cur.execute("SELECT vec FROM anime_profile WHERE subject_id = %s",
                    (BOCCHI,))
        row = cur.fetchone()
    assert row and row[0] is not None, \
        f"{BOCCHI} 没有 vec —— 先跑 scripts/build_embeddings.py"
    v = row[0]
    return (v.to_numpy() if hasattr(v, "to_numpy") else np.asarray(v)
            ).astype(np.float32)


def test_find_returns_self_as_top_hit(monkeypatch, conn, bocchi_vec):
    """拿一部作品自己的向量去查，它自己应该以余弦 ~1.0 排在最前面——顺带
    验证 FindHit 的字段（subject_id/name/air_year/match）没有错位。
    """
    monkeypatch.setattr(embed, "embed_query", lambda *a, **k: bocchi_vec)
    hits = find_mod.find(conn, "随便什么描述，反正 embed_query 会被 mock 掉")
    assert hits, "召回为空"
    top = hits[0]
    assert top.subject_id == BOCCHI
    assert top.match > 0.99
    assert top.air_year == 2022


def test_find_respects_top_k(monkeypatch, conn, bocchi_vec):
    monkeypatch.setattr(embed, "embed_query", lambda *a, **k: bocchi_vec)
    hits = find_mod.find(conn, "查询", top_k=3)
    assert len(hits) <= 3


def test_find_empty_recall_returns_empty_list_not_error(monkeypatch, conn):
    """R.score() 召回为空时，find() 必须返回 [] 而不是抛异常——与
    find_gate() 判"否"时的套话是两回事：这里连语义检索这一步都没跑出结果，
    调用方（main.py 的 find 分支）靠这个空列表判断要不要回落。
    """
    monkeypatch.setattr(embed, "embed_query",
                        lambda *a, **k: np.zeros(1024, dtype=np.float32))
    monkeypatch.setattr(R, "score", lambda *a, **k: [])
    hits = find_mod.find(conn, "任意查询")
    assert hits == []


def test_find_forwards_retries_and_timeout_to_embed_query(monkeypatch, conn):
    """🚨 I.4 记过的教训：请求路径必须传短重试预算，不能用离线批量任务的
    默认值——那次实测把延迟放大到 883 秒。这里锁死 find() 确实把
    retries/timeout 转发给了 embed_query()，不是悄悄丢在半路。
    """
    captured = {}

    def fake_embed_query(text, key=None, *, retries=None, timeout=None):
        captured["retries"] = retries
        captured["timeout"] = timeout
        return np.zeros(1024, dtype=np.float32)

    monkeypatch.setattr(embed, "embed_query", fake_embed_query)
    monkeypatch.setattr(R, "score", lambda *a, **k: [])
    find_mod.find(conn, "查询", retries=2, timeout=12.0)
    assert captured == {"retries": 2, "timeout": 12.0}

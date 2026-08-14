"""接口层冒烟测试。

⚠️ 与 test_parity.py 分工不同：那边验的是**打分算得对不对**，
   这边验的是**HTTP 外壳有没有把结果拼错** —— 响应组装、校验、错误码。
   打分正确但响应组装错了（字段错位、KeyError、顺序被打乱），
   test_parity 一个都发现不了。

需要能连到 Neon，且已跑过 scripts/build_tag_vectors.py。
"""

import pytest
from fastapi.testclient import TestClient

from server.main import API, app
from src import db


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def db_conn():
    """直连库，用于校验接口返回与库里的派生列一致（如 mmr_rank 的排序）。"""
    with db.connect() as conn:
        yield conn


@pytest.fixture(scope="module")
def answers(client):
    """拿真实题目造一份作答，覆盖全部四种 choice。"""
    items = client.get(f"{API}/questionnaire", params={"n": 6}).json()["items"]
    assert len(items) >= 5, "问卷题目太少，后面的断言没意义"
    return [
        {"subject_id": items[0]["subject_id"], "choice": "seen", "score": 9},
        {"subject_id": items[1]["subject_id"], "choice": "seen", "score": 5},
        {"subject_id": items[2]["subject_id"], "choice": "wish"},
        {"subject_id": items[3]["subject_id"], "choice": "pass"},
        {"subject_id": items[4]["subject_id"], "choice": "skip"},
    ]


def test_health(client):
    b = client.get(f"{API}/health").json()
    assert b["status"] == "ok", "tag_vec 未回填 —— 先跑 scripts/build_tag_vectors.py"
    # 回填完整性：只有零向量作品才允许缺 tag_vec
    assert b["catalog_size"] - b["with_tag_vec"] < 200


def test_questionnaire_folds_and_orders(client, db_conn):
    items = client.get(f"{API}/questionnaire", params={"n": 20}).json()["items"]
    assert len(items) == 20
    ids = [i["subject_id"] for i in items]
    assert len(set(ids)) == len(ids), "折叠后不该出现重复系列"

    # ⚠️ **不再断言「按热度降序」**（2026-08-14 改）。选题已从纯热度换成
    #    MMR 多样性排序 —— 纯热度选出的 30 题两两余弦均值 0.4552，比随机抽
    #    （0.3627）还冗余，问 30 题拿不到 30 题的信息量。热度降序恰恰是要打破的。
    with db_conn.cursor() as cur:
        cur.execute("SELECT subject_id, mmr_rank FROM anime_profile "
                    "WHERE subject_id = ANY(%s)", (ids,))
        rank = dict(cur.fetchall())
    ranks = [rank.get(i) for i in ids]
    assert all(r is not None for r in ranks), \
        "问卷选出了没有 mmr_rank 的作品 —— build_clusters.py 没跑或候选池口径漂移了"
    assert ranks == sorted(ranks), "选题应按 mmr_rank 升序"

    # 多样性不能以牺牲热度为代价：题目仍须是用户可能看过的作品。
    # 实测 MMR λ=0.5 的中位热度 41,040，纯热度是 44,146 —— 几乎不掉。
    assert sorted(i["done"] for i in items)[len(items) // 2] > 5_000, \
        "中位热度过低，MMR 的 λ 可能偏小，问卷会问到用户没看过的冷门作"


def test_questionnaire_second_round(client):
    """多次作答：把第一轮的题传进 exclude，应拿到不重复且仍连续的下一批。"""
    first = client.get(f"{API}/questionnaire", params={"n": 30}).json()["items"]
    ids1 = [i["subject_id"] for i in first]

    second = client.get(f"{API}/questionnaire", params={
        "n": 30, "exclude": ",".join(map(str, ids1))}).json()["items"]
    ids2 = [i["subject_id"] for i in second]

    assert len(ids2) == 30, "第二轮题目不足 —— 候选序列缓存太短"
    assert not (set(ids1) & set(ids2)), "第二轮出现了第一轮问过的作品"

    # ⚠️ 第二轮不是「随便往下顺延」：MMR 贪心序的第 31 位本就是
    #    「已选前 30 位的前提下信息增量最大的那一部」，所以位次必须严格接续。
    assert ids2 == [i["subject_id"] for i in
                    client.get(f"{API}/questionnaire", params={"n": 60}
                               ).json()["items"][30:]]

    # 多轮之后仍应是用户可能看过的作品，不能退化成冷门番
    assert sorted(i["done"] for i in second)[15] > 3_000

    # 排除集不该污染缓存：不带 exclude 再问一次，结果应与第一轮完全一致
    again = client.get(f"{API}/questionnaire", params={"n": 30}).json()["items"]
    assert [i["subject_id"] for i in again] == ids1

    assert client.get(f"{API}/questionnaire",
                      params={"exclude": "1,abc"}).status_code == 422


def test_questionnaire_survives_cache_exhaustion(client):
    """排除数超过候选序列缓存时，必须退回查库而不是静默返回空问卷。

    ⚠️ 缓存只有 _POOL_CACHE 条，库里却有 4,439 条可问。不做兜底的话，
       答满缓存的用户会拿到 200 + total=0，看不出还有几千部可问 —— 静默失败。
    """
    from server.main import _POOL_CACHE, _questions

    client.get(f"{API}/questionnaire", params={"n": 1})       # 触发缓存填充
    pool = [q.subject_id for q in next(iter(_questions.values()))]
    assert len(pool) == _POOL_CACHE

    got = client.get(f"{API}/questionnaire", params={
        "n": 30, "exclude": ",".join(map(str, pool))}).json()["items"]
    assert len(got) == 30, "缓存耗尽后没有退回查库"
    assert not ({i["subject_id"] for i in got} & set(pool)), "兜底路径漏了排除集"


def test_recommend_shape_and_order(client, answers):
    b = client.post(f"{API}/recommend", json={"answers": answers, "top_k": 10}).json()
    # skip 不产生记录
    assert b["used_ratings"] == 4

    items = b["items"]
    assert items, "有 4 条有效信号却没出推荐"
    # ⚠️ 契约：列表恒按 rank_score 降序。前端直接按顺序渲染，不重排。
    scores = [x["rank_score"] for x in items]
    assert scores == sorted(scores, reverse=True)

    seen_ids = {a["subject_id"] for a in answers if a["choice"] in ("seen", "pass")}
    wish_id = next(a["subject_id"] for a in answers if a["choice"] == "wish")
    got = {x["subject_id"] for x in items}
    assert not (got & seen_ids), "「看过」和「不感兴趣」的必须剔除"
    assert wish_id not in seen_ids                   # 「想尝试」不在剔除集里

    for x in items:
        assert -1.0 <= x["match"] <= 1.0
        assert 0.0 <= x["quality"] <= 10.0
        assert x["name"] and x["subject_id"]
        # 响应组装的重点：这几个字段来自额外一次查询，最容易错位
        assert x["done"] >= 0
        assert x["bgm_score"] is None or 0 < x["bgm_score"] <= 10
        assert isinstance(x["reasons"], list)


def test_recommend_min_score_floor(client, answers):
    """默认下限应挡住低分作品；显式关闭后允许出现。"""
    body = {"answers": answers, "top_k": 30, "rank_by": "match"}
    on = client.post(f"{API}/recommend", json=body).json()["items"]
    assert all(x["bgm_score"] is None or x["bgm_score"] >= 3.5 for x in on)

    off = client.post(f"{API}/recommend", json={**body, "min_score": None})
    assert off.status_code == 200          # 关闭是合法请求，不是错误


def test_recommend_empty_and_validation(client):
    b = client.post(f"{API}/recommend", json={"answers": []}).json()
    assert b == {"items": [], "used_ratings": 0, "rank_by": "blend"}

    # choice='seen' 缺分数 → 422（业务校验，不是 pydantic 校验）
    r = client.post(f"{API}/recommend",
                    json={"answers": [{"subject_id": 1, "choice": "seen"}]})
    assert r.status_code == 422
    # 越界参数 → 422（pydantic 校验）
    for bad in ({"top_k": 0}, {"blend_alpha": 2}, {"min_score": 99},
                {"rank_by": "nope"}, {"mode": "nope"}):
        r = client.post(f"{API}/recommend", json={"answers": [], **bad})
        assert r.status_code == 422, f"{bad} 应当被拒绝"


def test_search_two_stage(client):
    """BM25 正常命中，拼错时走 trgm 兜底 —— via 字段要如实反映走了哪条路。"""
    hits = client.get(f"{API}/search", params={"q": "进击的巨人", "limit": 5}).json()
    assert hits and all(h["via"] == "tsv" for h in hits)

    fuzzy = client.get(f"{API}/search", params={"q": "fate zeero", "limit": 5}).json()
    assert fuzzy and fuzzy[0]["via"] == "trgm"
    ids = [h["subject_id"] for h in fuzzy]
    assert len(set(ids)) == len(ids), "别名去重失效 —— 一部作品会因多条别名刷屏"

    assert client.get(f"{API}/search", params={"q": ""}).status_code == 422


def test_anime_detail(client):
    d = client.get(f"{API}/anime/328609").json()          # 孤独摇滚！
    assert d["subject_id"] == 328609
    assert d["air_year"] == 2022 and d["form"] == "TV"
    # 只回落在词表内的题材 tag —— 不能把制作公司/声优/年份吐给前端
    assert d["tags"] and all(t not in ("2022", "CloverWorks") for t in d["tags"])
    assert "CloverWorks" in d["studios"]

    assert client.get(f"{API}/anime/999999999").status_code == 404

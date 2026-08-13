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


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


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


def test_questionnaire_folds_and_orders(client):
    items = client.get(f"{API}/questionnaire", params={"n": 20}).json()["items"]
    assert len(items) == 20
    done = [i["done"] for i in items]
    assert done == sorted(done, reverse=True), "选题应按热度降序"
    ids = [i["subject_id"] for i in items]
    assert len(set(ids)) == len(ids), "折叠后不该出现重复系列"


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

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


def test_recommend_fusion_weights(client, answers):
    """P1 融合权重。⚠️ 这是第 5 周跑四条 baseline 的入口，必须可用。"""
    body = {"answers": answers, "top_k": 5}
    tag = client.post(f"{API}/recommend",
                      json={**body, "w_tag": 1, "w_emb": 0, "w_staff": 0}).json()["items"]
    emb = client.post(f"{API}/recommend",
                      json={**body, "w_tag": 0, "w_emb": 1, "w_staff": 0}).json()["items"]
    assert tag and emb
    # 两条 baseline 必须真的不同 —— 相同说明权重没被传下去
    assert [x["subject_id"] for x in tag] != [x["subject_id"] for x in emb]

    # ⚠️ 部分指定会组合出谁也没想要的权重（另外两个取默认值），必须拒绝
    assert client.post(f"{API}/recommend",
                       json={**body, "w_tag": 1}).status_code == 422
    assert client.post(f"{API}/recommend",
                       json={**body, "w_tag": 0, "w_emb": 0,
                             "w_staff": 0}).status_code == 422


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


# ── /api/ask（流程 C，阶段 05）────────────────────────────────────
# ⚠️ **这里只测不打外部 API 的那几条状态。** state=ok 会真的调
#    embedding + rerank + LLM：慢（3–6 秒）、要花钱、且 LLM 输出不确定 ——
#    把它放进 CI 会让测试变成一个不稳定的付费探针。
#    「答得对不对」是第 5 周离线评测的事，不是冒烟测试的事。

def test_ask_unknown_short_circuits(client):
    """认不出实体时必须短路，且**不能**假装答出来。

    ⚠️ 这条守的是 G.4 状态④/③ 那个设计意图：检索为空还把问题丢给 LLM，
       它会用训练记忆流畅地编一个，绕过整条 RAG 链路。
    """
    r = client.post(f"{API}/ask", json={"question": "明天几点开会"})
    assert r.status_code == 200, "业务状态不该做成 4xx"
    body = r.json()
    assert body["state"] == "unknown"
    assert body["chunks"] == [] and body["series_root"] is None
    assert body["answer"], "要给用户一句说明，不能返回 None 让前端自己编"


def test_ask_ambiguous_asks_back(client):
    """跨作品重名必须反问，且候选按作品去重。

    ⚠️ 「拉姆」实测撞 4 部作品（Re:0 / 战车讲座 / 影宅 / 超次元游戏 海王星）。
       G.4：猜错的代价不对称 —— 反问多花一次点击，猜错是自信地讲了
       另一部作品的剧情，而用户很可能看不出来。
    """
    r = client.post(f"{API}/ask", json={"question": "拉姆是谁？"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "ambiguous"
    assert body["chunks"] == [], "反问阶段不该返回任何语料"
    roots = [c["series_root"] for c in body["candidates"]]
    assert len(roots) > 1, "只有一个候选就不该判成歧义"
    assert len(roots) == len(set(roots)), "候选必须按 series_root 去重"


def test_ask_latin_substring_not_a_mention(client):
    """纯拉丁别名必须落在词边界上。

    🚨 回归测试：实测「帮我写一个快速排序的 Python 实现」曾命中 py / hon、
       「怎么把 Excel 表格导出成 CSV」曾命中 el / ex —— 从英文词**中间**
       截出的两字符片段撞上了短别名，于是一个与动画无关的问题被自信地
       解析成了某部作品。见 retrieve._latin_word_boundary()。
    """
    for q in ("帮我写一个快速排序的 Python 实现", "怎么把 Excel 表格导出成 CSV"):
        body = client.post(f"{API}/ask", json={"question": q}).json()
        assert body["state"] == "unknown", f"{q!r} 被误判成 {body['state']}"


def test_ask_validation(client):
    """入参校验。question 有长度上下界，top_k 有范围。"""
    for bad in ({}, {"question": ""}, {"question": "x" * 300},
                {"question": "测试", "top_k": 0},
                {"question": "测试", "top_k": 99}):
        assert client.post(f"{API}/ask", json=bad).status_code == 422, bad


# ── /api/related（结构化关联查询）─────────────────────────────────
# ⚠️ **零模型调用**，所以这两条可以放心进 CI —— 与 /ask 的 state=ok 不同。

def test_related_finds_same_author(client):
    """🚨 回归：/ask 答不了「作者还画过什么」，而且答不了是对的。

    流程 C 的召回写死了 WHERE series_root = X（第 15 节原则 4），
    按设计就看不到别的作品的 chunk。实测「冰之城墙的作者还画过其他漫画吗」
    → 「资料中没有提到」，而 staff 列里一条 SQL 就查得到《相反的你和我》。
    """
    r = client.get(f"{API}/related",
                   params={"series_root": 535669, "role": "原作"})
    assert r.status_code == 200
    body = r.json()
    titles = [i["name_cn"] or i["name"] for i in body["items"]]
    assert "相反的你和我" in titles, f"没查到同作者作品：{titles}"
    assert all(i["via_role"] == "原作" for i in body["items"])
    # ⚠️ 结果不含本系列 —— 续作不算「另一部作品」
    assert all(i["series_root"] != 535669 for i in body["items"])


def test_related_validation(client):
    assert client.get(f"{API}/related",
                      params={"series_root": 999999999}).status_code == 404
    assert client.get(f"{API}/related",
                      params={"series_root": 535669,
                              "role": "不存在的岗位"}).status_code == 422
    assert client.get(f"{API}/related").status_code == 422


# ── /api/ask 的多轮对话（2026-08-19）─────────────────────────────
# ⚠️ 这两条**零模型调用**，所以能进 CI：ambiguous 在 retrieve() 里就返回了，
#    走不到 embed/rerank/LLM（与 state=ok 的用例不同，那种不放 CI）。

def test_ask_ambiguous_candidates_are_distinguishable(client):
    """🚨 回归：同名重制版必须能被区分开，否则反问是死路。

    实测 bug：《多罗罗》1969(done=911) 与 2019(done=9450) 是两个 series_root，
    而反问的选项曾按**标题**去重 → 用户看到唯一一个「《多罗罗》」，无从选择。
    全库有 81 个这样的同名标题（忍者神龟×3 铁臂阿童木×3 狮子王×3…）。
    """
    r = client.post(f"{API}/ask", json={"question": "《多罗罗》的片头曲是什么？"})
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "ambiguous"
    cands = b["candidates"]
    assert len(cands) >= 2, f"应给出多个候选，实际 {cands}"
    roots = [c["series_root"] for c in cands]
    assert len(set(roots)) == len(roots), "候选必须按 series_root 去重，不能重复"
    # 标题相同的候选，必须能靠年份区分
    same = [c for c in cands if c["title"] == cands[0]["title"]]
    if len(same) > 1:
        years = [c["year"] for c in same]
        assert all(y is not None for y in years), f"同名候选缺年份：{same}"
        assert len(set(years)) == len(years), f"同名候选年份也相同：{same}"


def test_resolve_in_scope_pins_the_scope(db_conn):
    """结构化续问：客户端回传 series_root 后，作用域被钉死。

    ⚠️ 不走 HTTP 端点，因为 state=ok 会真的调 embed/rerank/LLM。
       这里只验解析层，零外部调用。
    """
    from src import retrieve

    q = "《多罗罗》的片头曲是什么？"
    assert retrieve.resolve(db_conn, q).state is retrieve.State.AMBIGUOUS
    for root in (107454, 240838):          # 1969 / 2019
        res = retrieve.resolve_in_scope(db_conn, q, root)
        assert res.series_root == root, "回传的 root 必须被无条件采纳"
        assert res.state is not retrieve.State.AMBIGUOUS, "钉死之后不该再反问"


# ── /api/season（按档期浏览）──────────────────────────────────────
# ⚠️ 零模型调用、无个性化 —— 「这个季度在播什么」谁来问答案都一样。

def test_season_documented_window(client):
    """第四部分的实测基准：2016 年 7 月季 = 2016-06-24 ~ 2016-10-01 共 134 部，
    头名《你的名字。》done=57,748。窗口两端必须逐日吻合；
    总数只做下限断言 —— 季度更新灌新数据后精确值会漂。
    """
    b = client.get(f"{API}/season", params={"year": 2016, "month": 7}).json()
    assert (b["year"], b["month"]) == (2016, 7)
    assert b["window_start"] == "2016-06-24"
    assert b["window_end"] == "2016-10-01"
    assert b["total"] >= 100, f"2016 夏季实测 134 部，现在只有 {b['total']}"

    items = b["items"]
    dones = [i["done"] for i in items]
    assert dones == sorted(dones, reverse=True), "应按 fav_done 降序"
    assert all(b["window_start"] <= i["air_date"] < b["window_end"]
               for i in items), "有条目落在窗口外"
    top = items[0]["name_cn"] or items[0]["name"]
    assert top == "你的名字。", f"头名应是文档实测的《你的名字。》，得到 {top}"


def test_season_month_normalizes_to_cour(client):
    """传 8 月要归一化到 7 月番 —— 用户不知道「季度月」是 1/4/7/10。"""
    a = client.get(f"{API}/season", params={"year": 2016, "month": 8}).json()
    b = client.get(f"{API}/season", params={"year": 2016, "month": 7}).json()
    assert a["month"] == 7
    assert (a["window_start"], a["window_end"]) == \
           (b["window_start"], b["window_end"])


def test_season_defaults_to_current_cour(client):
    """缺省参数 = 今天所在季度。「十年前的这个季度」只要传 year=今年-10。"""
    b = client.get(f"{API}/season").json()
    assert b["month"] in (1, 4, 7, 10)
    assert b["window_start"] <= b["window_end"]

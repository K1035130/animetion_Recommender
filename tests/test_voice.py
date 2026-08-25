"""声优配役查询的测试。

⚠️ 分两层，与 test_api.py / test_retrieve.py 的分工一致：
   · wants() 是纯函数，不连库 —— 意图路由错了是**静默**的（用户以为系统笨，
     而不知道是分错了路），所以它值得被逐条钉死。
   · 其余走 HTTP，验的是响应组装和折叠规则有没有拼错。

需要能连到 Neon，且已跑过 sql/009_voice_role.sql + scripts/build_voice_roles.py。
"""

import pytest
from fastapi.testclient import TestClient

from server.main import API, app
from src import llm, voice


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ============================================================
# 意图路由（纯函数）
# ============================================================
@pytest.mark.parametrize("q", [
    "花泽香菜配过哪些角色？",
    "钉宫理惠配了什么角色",
    "樱井孝宏的配音作品有哪些",
])
def test_wants_person(q):
    assert voice.wants(q) == "person"


@pytest.mark.parametrize("q", [
    "银魂里神乐是谁配的",
    "这个角色的声优是谁",
    "路飞的cv是谁",
])
def test_wants_character(q):
    assert voice.wants(q) == "character"


@pytest.mark.parametrize("q", [
    "推荐几部科幻番",
    "《进击的巨人》的结局是什么",
    "十年前的这个季度在播什么",
])
def test_wants_none(q):
    """⚠️ 不该命中的必须返回 None —— 路由错了是静默失败。"""
    assert voice.wants(q) is None


def test_wants_character_beats_person():
    """「谁配的」比「配过」更具体，两组词同时命中时必须判 character。"""
    assert voice.wants("银魂里神乐是谁配的角色") == "character"


# ============================================================
# 接口
# ============================================================
def test_voice_lookup_by_chinese_name(client):
    """中文名要能对上库里的日文原名 —— 这是 sql/009 把声优灌进 alias 的全部理由。"""
    r = client.get(f"{API}/voice", params={"name": "花泽香菜", "limit": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "花澤香菜"
    assert d["name_cn"] == "花泽香菜"
    assert d["n_roles"] > 300          # 实测 466，留足余量防数据微调时假红
    assert len(d["items"]) == 5


def test_voice_unknown_name_404(client):
    r = client.get(f"{API}/voice", params={"name": "这个人不存在xyz"})
    assert r.status_code == 404


def test_voice_name_too_short_422(client):
    """1 个字的「人名」一律拒掉 —— 否则子串枚举会命中一堆噪声。"""
    assert client.get(f"{API}/voice", params={"name": "花"}).status_code == 422


def test_roles_folded_by_character(client):
    """🚨 同一角色只列一次。

    不折的话钉宫理惠会连出「逢坂大河《龙与虎》」「逢坂大河《龙与虎OVA》」——
    因为 OVA 自成一个 series_root（第四部分挂着的 rt=12 未折叠）。
    """
    r = client.get(f"{API}/voice", params={"name": "钉宫理惠", "limit": 20})
    ids = [it["character_id"] for it in r.json()["items"]]
    assert len(ids) == len(set(ids)), "同一 character_id 出现了多次"


def test_representative_work_prefers_lead_role(client):
    """🚨 选代表作时 role_type 必须压过热度。

    「神乐」在《齐木楠雄的灾难 第二季》(done=27,828) 是客串、在《银魂》
    (done=18,539) 是主角。只按热度会选中客串那条，接着又因 role_type≠1
    被排到列表末尾 —— 结果钉宫理惠的代表作里整个看不到神乐。
    """
    r = client.get(f"{API}/voice", params={"name": "钉宫理惠", "limit": 20})
    hit = [it for it in r.json()["items"] if it["character_name"] == "神乐"]
    assert hit, "神乐不在钉宫理惠的前 20 个代表役里"
    assert hit[0]["title"].startswith("银魂")
    assert hit[0]["role_type"] == 1


def test_lead_roles_come_first(client):
    """主角优先排序：前几条不该出现客串。"""
    items = client.get(f"{API}/voice",
                       params={"name": "樱井孝宏", "limit": 10}).json()["items"]
    assert all(it["role_type"] == 1 for it in items[:5])


def test_role_type_is_not_a_filter(client):
    """⚠️ role_type 只用于排序**不用于过滤** —— n_roles 是未截断的总数，
    必须远大于 limit，否则说明哪里悄悄把配角滤掉了。"""
    d = client.get(f"{API}/voice", params={"name": "樱井孝宏", "limit": 10}).json()
    assert d["n_roles"] > 100


# ============================================================
# 排序口径（2026-08-25）
# ============================================================

@pytest.mark.parametrize("q", [
    "花泽香菜最近配过什么角色",
    "钉宫理惠近期有哪些作品",
    "他今年配了谁",
    "这几年她配过什么",
])
def test_wants_recent_true(q):
    assert voice.wants_recent(q)


@pytest.mark.parametrize("q", [
    "花泽香菜配过哪些角色",
    "钉宫理惠的代表作有哪些",
    "银魂里神乐是谁配的",
    # ⚠️ 有意不收「新番」——「她在新番里配了谁」问的是具体作品不是时间段。
    "她在新番里配了谁",
])
def test_wants_recent_false(q):
    assert not voice.wants_recent(q)


def test_weighted_heat_keeps_lead_over_hot_cameo():
    """🚨 纯热度排序会让客串役屠榜 —— 这条把加权口径钉死。

    2026-08-25 实测：改成纯 `fav_done` 降序后，钉宫理惠的「神乐」从第 3 掉到
    第 21，前 12 条里 9 条是配角/客串。根因是热门作品的角色**总数**远多于
    冷门作品，纯热度等价于"按作品热度列角色"，而用户问的是配过哪些**角色**。
    """
    cameo = voice.Role(character_id=1, character_name="甲", series_root=1,
                       title="超热门番", air_year=2020, role_type=3,
                       fav_done=100_000)
    lead = voice.Role(character_id=2, character_name="乙", series_root=2,
                      title="中等热度番", air_year=2020, role_type=1,
                      fav_done=20_000)
    assert voice._weighted_heat(lead) > voice._weighted_heat(cameo)


def test_recent_order_is_year_desc(client):
    """order=recent 必须是年份降序 —— 这是「最近配了什么」的全部依据。"""
    items = client.get(f"{API}/voice",
                       params={"name": "花泽香菜", "limit": 15,
                               "order": "recent"}).json()["items"]
    years = [it["air_year"] for it in items if it["air_year"] is not None]
    assert years == sorted(years, reverse=True)
    # 年份未知的必须垫底，不能因为 `or 0` 写反而冒到最前
    seen_none = False
    for it in items:
        if it["air_year"] is None:
            seen_none = True
        elif seen_none:
            pytest.fail("air_year 为 None 的记录排到了有年份的记录前面")


def test_recent_and_popular_differ(client):
    """两种排序必须给出不同的列表，否则 order 参数根本没接上去。"""
    def ids(order):
        return [it["character_id"] for it in client.get(
            f"{API}/voice",
            params={"name": "花泽香菜", "limit": 15, "order": order},
        ).json()["items"]]
    assert ids("popular") != ids("recent")


def test_bad_order_is_rejected(client):
    r = client.get(f"{API}/voice",
                   params={"name": "花泽香菜", "order": "heat"})
    assert r.status_code == 422


def test_context_states_total_and_listed_counts():
    """⚠️ 资料必须同时写明「库内共 N 条」和「下面是 M 条」。

    只写一个数的话模型会把"列了 M 条"当成"总共就 M 条"，进而说出
    「这位声优作品不多」这类与 n_roles 直接矛盾的话。
    """
    p = voice.Person(person_id=1, name="X", name_cn=None, n_roles=478)
    roles = [voice.Role(character_id=1, character_name="甲", series_root=1,
                        title="作品", air_year=2020, role_type=1,
                        fav_done=100)]
    _sec, text = voice.as_context(p, roles)
    assert "478" in text
    assert "1 条" in text


# ============================================================
# /ask 的 voice 分支：LLM 组织回答 + 失败回落（2026-08-25）
# ============================================================

def _no_intent_call(monkeypatch):
    """把意图校验钉成 voice，避免用例打真实 LLM 接口。"""
    monkeypatch.setattr(llm, "classify_intent",
                        lambda q: ("voice", llm.INTENT_PROVIDER))


def test_ask_voice_answer_comes_from_llm(client, monkeypatch):
    _no_intent_call(monkeypatch)
    monkeypatch.setattr(
        llm, "voice_answer",
        lambda q, ctx, **kw: ("这是 LLM 写的回答", llm.VOICE_PROVIDER))

    d = client.post(f"{API}/ask", json={
        "question": "花泽香菜配过哪些角色", "route": "voice"}).json()

    assert d["route"] == "voice"
    assert d["answer"] == "这是 LLM 写的回答"
    assert d["meta"]["llm"]
    assert d["meta"]["zero_model"] is False
    # 🚨 **结构化列表必须照常返回** —— 生成会丢条目，别让 LLM 成为唯一出口。
    assert len(d["voice"]["items"]) > 3


def test_ask_voice_falls_back_to_table_when_llm_fails(client, monkeypatch):
    """🚨 LLM 挂了要退回配役表并返回 200，**不是 503**。

    与流程 C 不同：那里没有模型就真的没有回答，而这里资料已经拿到了，
    LLM 只负责组织语言。
    """
    _no_intent_call(monkeypatch)

    def boom(q, ctx, **kw):
        raise llm.LLMError("生成挂了")
    monkeypatch.setattr(llm, "voice_answer", boom)

    r = client.post(f"{API}/ask", json={
        "question": "花泽香菜配过哪些角色", "route": "voice"})
    assert r.status_code == 200
    d = r.json()
    assert d["route"] == "voice"
    assert "在库内共有" in d["answer"]        # 回落成了配役表本身
    assert d["meta"]["llm_error"] == "LLMError"
    assert len(d["voice"]["items"]) > 3


def test_ask_voice_recent_switches_order(client, monkeypatch):
    """「最近」要真的切到年份序 —— 判据是 meta，不是靠肉眼看列表。"""
    _no_intent_call(monkeypatch)
    monkeypatch.setattr(llm, "voice_answer",
                        lambda q, ctx, **kw: ("ok", llm.VOICE_PROVIDER))

    def order_of(question):
        return client.post(f"{API}/ask", json={
            "question": question, "route": "voice"}).json()["meta"]["voice_order"]

    assert order_of("花泽香菜最近配过什么角色") == voice.ORDER_RECENT
    assert order_of("花泽香菜配过哪些角色") == voice.ORDER_POPULAR

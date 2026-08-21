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
from src import voice


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

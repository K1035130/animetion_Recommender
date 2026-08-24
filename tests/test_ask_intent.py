"""意图校验层（llm.classify_intent）与找番门控（llm.find_gate）接入
POST /api/ask 的路由 wiring —— 2026-08-24 新增的分支。

测的是"选对了下一步"，不是"LLM 判断准不准"（那是打分表的事，见
docs/eval-find-gate-sample.md，classify_intent 那部分的打分表还没出）。

⚠️ 同一条纪律：全部 monkeypatch 掉 llm.classify_intent / llm.find_gate /
find.find，不打真实外部 API。这里要锁的正是 CLAUDE.md 记录过的几个
真实回归（都是端到端手测抓到的，这次补成自动化用例）：
  · auto 模式下 intent 与 router.classify() 不一致时要采用 intent
  · season/find 按钮选错时不能"自信地答非所问"，要拦下来回落到 ask，
    且回落原因必须出现在 answer 里（不能只放 route_reason）
  · **voice 分支的空结果回落必须完全不看 intent**——这条本身就是一次
    真实回归（off_topic 短路曾覆盖掉更准确的"没有找到这个名字的声优"）
  · 校验/门控本身挂了（LLMError）要退回"信任原路由"，不能让整条请求跟着炸

问句一律选**能安全触发 state=unknown 的那几条**（"明天几点开会"，与
test_api.py::test_ask_unknown_short_circuits 同一条）——resolve() 在这种
问句上认不出任何实体，retrieve.ask() 会在调 embedding/LLM 之前就短路
返回，所以这里的 DB 调用是真实的，但不会产生额外的外部 API 调用。
"""

import pytest
from fastapi.testclient import TestClient

from server.main import API, app
from src import find as find_mod
from src import llm

UNKNOWN_Q = "明天几点开会"     # 全库都认不出的问句，安全触发 state=unknown


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _fake_intent(value):
    def fake(query):
        return value, llm.INTENT_PROVIDER
    return fake


def _fake_intent_raises():
    def fake(query):
        raise llm.LLMError("intent 挂了")
    return fake


def _fake_find_hits():
    return [find_mod.FindHit(subject_id=328609, name="孤独摇滚！",
                             air_year=2022, match=0.8)]


# ============================================================
# auto 模式
# ============================================================

def test_auto_off_topic_short_circuits(client, monkeypatch):
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("off_topic"))
    r = client.post(f"{API}/ask", json={"question": "随便一句话", "route": "auto"})
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "unknown"
    assert b["route"] == "ask"
    assert b["meta"]["intent"] == "off_topic"
    assert "关于动画" in b["answer"]


def test_auto_intent_overrides_router_guess(client, monkeypatch):
    """🚨 [7] 那道回归（CLAUDE.md「流程 B 找番」一节）的正面用例：
    router.classify() 用触发词猜不出 find（它从不会猜这个分支），
    LLM 判断更细时要采用 LLM 的判断，而不是保留 router 的原始猜测。
    """
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("find"))
    monkeypatch.setattr(find_mod, "find", lambda *a, **k: _fake_find_hits())
    r = client.post(f"{API}/ask", json={
        "question": "一句router会猜成ask的普通描述", "route": "auto"})
    assert r.status_code == 200
    b = r.json()
    assert b["route"] == "find"
    assert b["find"]["items"][0]["subject_id"] == 328609


def test_auto_intent_failure_trusts_original_route(client, monkeypatch):
    """校验本身挂了不能拖垮整条请求——退回"信任原路由"，
    与「一道校验挂了就当没跑过」是同一条纪律（find_gate 挂了同理，
    见下面 test_find_gate_failure_falls_back_to_generic_unknown）。
    """
    monkeypatch.setattr(llm, "classify_intent", _fake_intent_raises())
    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "auto"})
    assert r.status_code == 200
    b = r.json()
    # retrieve.ask() 正常跑完给出 state=unknown，没有被误短路成 off_topic
    assert b["state"] == "unknown"
    assert b["meta"].get("intent") != "off_topic"


# ============================================================
# 按钮模式：season
# ============================================================

def test_season_button_off_topic_short_circuits(client, monkeypatch):
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("off_topic"))
    r = client.post(f"{API}/ask", json={"question": "随便问问", "route": "season"})
    assert r.status_code == 200
    b = r.json()
    assert b["meta"].get("intent") == "off_topic"
    assert b["season"] is None


def test_season_button_mismatch_falls_back_with_reason_in_answer(client, monkeypatch):
    """🚨 回归：season 按钮问剧情问题曾无视问句内容直接展示当季新番列表。
    现在应该被拦下来、回落到 ask，且回落原因必须出现在 answer 里——
    用户主要看 answer，route_reason 他不会看。
    """
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("ask"))
    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "season"})
    assert r.status_code == 200
    b = r.json()
    assert b["route"] == "ask"
    assert b["season"] is None
    assert "不像是新番/档期类问题" in b["answer"]


# ============================================================
# 按钮模式：find
# ============================================================

def test_find_button_off_topic_short_circuits(client, monkeypatch):
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("off_topic"))
    r = client.post(f"{API}/ask", json={"question": "随便问问", "route": "find"})
    assert r.status_code == 200
    b = r.json()
    assert b["meta"].get("intent") == "off_topic"
    assert b["find"] is None


def test_find_button_mismatch_falls_back_with_reason_in_answer(client, monkeypatch):
    """🚨 回归：find 按钮问声优问题曾硬凑一堆题材相近的番，没有回答问题。"""
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("voice"))
    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "find"})
    assert r.status_code == 200
    b = r.json()
    assert b["route"] == "ask"
    assert b["find"] is None
    assert "不像是找番类问题" in b["answer"]


# ============================================================
# 按钮模式：voice —— 关键回归：空结果回落必须完全不看 intent
# ============================================================

def test_voice_button_empty_result_ignores_intent(client, monkeypatch):
    """🚨 2026-08-24 实测抓到的真实回归：voice 分支空结果时若拿 intent
    去覆盖，"不存在的声优xyzq" 这类问句会被 classify_intent 误判成
    off_topic，把已经准确的"没有找到这个名字的声优"换成更含糊的离题提示，
    是纯倒退。voice 分支必须完全不受这里的 mock 影响。
    """
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("off_topic"))
    r = client.post(f"{API}/ask",
                    json={"question": "不存在的声优xyzq", "route": "voice"})
    assert r.status_code == 200
    b = r.json()
    assert b["voice"] is None
    assert "没有找到这个名字的声优" in b["answer"]
    assert "关于动画的问题" not in b["answer"]


# ============================================================
# 显式 route=ask
# ============================================================

def test_explicit_ask_route_off_topic_short_circuits(client, monkeypatch):
    """auto 模式已经在最上面短路过一次；这里补的是显式 route=ask 时
    保持同样的行为——不能"点了按钮就不校验了"，前后不一致。
    """
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("off_topic"))
    r = client.post(f"{API}/ask", json={"question": "随便问问", "route": "ask"})
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "unknown"
    assert b["meta"]["intent"] == "off_topic"


# ============================================================
# 找番门控（llm.find_gate）：resolve() 完全没认出任何东西时的最后兜底
# ============================================================

def test_unknown_find_gate_true_returns_find_results(client, monkeypatch):
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("ask"))
    monkeypatch.setattr(llm, "find_gate", lambda q: (True, llm.PRIMARY))
    monkeypatch.setattr(find_mod, "find", lambda *a, **k: _fake_find_hits())
    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "ask"})
    assert r.status_code == 200
    b = r.json()
    assert b["route"] == "find"
    assert b["find"]["items"][0]["subject_id"] == 328609
    assert "仅供参考" in b["answer"]


def test_unknown_find_gate_false_returns_soft_prompt(client, monkeypatch):
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("ask"))
    monkeypatch.setattr(llm, "find_gate", lambda q: (False, llm.PRIMARY))
    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "ask"})
    assert r.status_code == 200
    b = r.json()
    assert b["state"] == "unknown"
    assert b["find"] is None
    assert "描述有些模糊不清" in b["answer"]


def test_find_gate_failure_falls_back_to_generic_unknown(client, monkeypatch):
    """门控本身挂了：不猜，退回原来的"没认出"套话，而不是让整条请求 500。"""
    monkeypatch.setattr(llm, "classify_intent", _fake_intent("ask"))

    def boom(q):
        raise llm.LLMError("find_gate 挂了")
    monkeypatch.setattr(llm, "find_gate", boom)

    r = client.post(f"{API}/ask", json={"question": UNKNOWN_Q, "route": "ask"})
    assert r.status_code == 200
    b = r.json()
    assert b["find"] is None
    assert b["state"] == "unknown"

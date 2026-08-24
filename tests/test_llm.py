"""src/llm.py 的分类式调用单测 —— find_gate() / classify_intent()。

⚠️ 与 test_api.py 顶部同一条纪律：**不打真实外部 API**（慢、要花钱、且非
   确定性）。这里全部用 monkeypatch 换掉网络层，只测本模块自己的逻辑：
   解析是否宽容但不默认为真、走的是哪个 provider、system prompt/max_tokens
   有没有接对。「答得准不准」不是这里的事——那是 docs/eval-find-gate-sample.md
   这类人工打分表要回答的问题；这里锁的是**代码逻辑不会被悄悄改坏**。
"""

import pytest

from src import llm


# ============================================================
# find_gate()
# ============================================================
# find_gate() 内部调 complete()，这里直接换掉 complete()——顺带验证它传的
# system prompt / max_tokens 是不是 FIND_GATE_SYSTEM / MAX_TOKENS_FIND_GATE，
# 不是别的任务的那一套（find_gate 与 classify_intent 都是新加的，容易在
# 复制粘贴时接错常量）。

def _fake_complete(reply: str):
    calls = []

    def fake(messages, *, max_tokens, allow_fallback=True):
        calls.append((messages, max_tokens, allow_fallback))
        return reply, llm.PRIMARY
    return fake, calls


@pytest.mark.parametrize("reply,expected", [
    ("是", True),
    ("是的，这句话在描述内容特征", True),   # 宽容解析：只看开头是不是"是"
    ("否", False),
    ("否，这是具体的作品名", False),
    ("", False),                          # 空回复：判不清就当"否"（失败要安全）
    ("这个问题很难说", False),              # 没有以"是"开头，一律当假
])
def test_find_gate_parses_leading_character(monkeypatch, reply, expected):
    fake, _ = _fake_complete(reply)
    monkeypatch.setattr(llm, "complete", fake)
    result, served = llm.find_gate("随便一个查询")
    assert result is expected
    assert served is llm.PRIMARY


def test_find_gate_wires_own_prompt_and_budget(monkeypatch):
    """回归：不能不小心复用 HYDE_SYSTEM/ANSWER_SYSTEM，或接错 max_tokens。"""
    fake, calls = _fake_complete("是")
    monkeypatch.setattr(llm, "complete", fake)
    llm.find_gate("在异世界开餐厅的故事")
    (messages, max_tokens, _allow), = calls
    assert messages[0] == {"role": "system", "content": llm.FIND_GATE_SYSTEM}
    assert messages[1] == {"role": "user", "content": "在异世界开餐厅的故事"}
    assert max_tokens == llm.MAX_TOKENS_FIND_GATE


def test_find_gate_propagates_llm_error(monkeypatch):
    """全部供应商失败时必须往上抛，不能吞掉伪装成"否"——
    调用方（main.py）靠这个异常区分"门控判否"和"门控本身挂了"，
    两者的兜底文案不一样（见 test_ask_intent.py 的对应用例）。
    """
    def boom(messages, *, max_tokens, allow_fallback=True):
        raise llm.LLMError("all providers down")
    monkeypatch.setattr(llm, "complete", boom)
    with pytest.raises(llm.LLMError):
        llm.find_gate("随便什么")


# ============================================================
# classify_intent()
# ============================================================
# ⚠️ 不经过 complete()/chain()，直接打 _post_with_retry(INTENT_PROVIDER, ...)
# ——这里换掉 _post_with_retry，顺带验证传的是 INTENT_PROVIDER 不是 PRIMARY。

def _fake_post_with_retry(reply: str):
    calls = []

    def fake(provider, messages, max_tokens):
        calls.append((provider, messages, max_tokens))
        return reply
    return fake, calls


@pytest.mark.parametrize("value", list(llm.INTENT_VALUES))
def test_classify_intent_recognizes_all_five_values(monkeypatch, value):
    fake, _ = _fake_post_with_retry(value)
    monkeypatch.setattr(llm, "_post_with_retry", fake)
    result, served = llm.classify_intent("随便一个问题")
    assert result == value
    assert served is llm.INTENT_PROVIDER


def test_classify_intent_tolerates_case_and_whitespace(monkeypatch):
    fake, _ = _fake_post_with_retry("  ASK\n")
    monkeypatch.setattr(llm, "_post_with_retry", fake)
    result, _served = llm.classify_intent("随便一个问题")
    assert result == "ask"


def test_classify_intent_unrecognized_reply_returns_none(monkeypatch):
    """解析失败必须是 None，不能猜一个默认值——调用方要能分清"判成
    off_topic"和"这道校验没跑成"是两种不同的业务状态（见 classify_intent
    的 docstring：这点与 find_gate()"判不清就当否"不是同一回事）。
    """
    fake, _ = _fake_post_with_retry("喵喵喵")
    monkeypatch.setattr(llm, "_post_with_retry", fake)
    result, served = llm.classify_intent("随便一个问题")
    assert result is None
    assert served is llm.INTENT_PROVIDER


def test_classify_intent_uses_intent_provider_not_primary(monkeypatch):
    """回归：这是每条请求都跑的分类任务，必须走独立的 Qwen3-8B，不能
    不小心接进 PRIMARY/FALLBACKS 那条问答链（会把成本拉高一个数量级，
    且首次实测不关思考时单次调用要 11.38 秒，见 INTENT_PROVIDER 的注释）。
    """
    fake, calls = _fake_post_with_retry("ask")
    monkeypatch.setattr(llm, "_post_with_retry", fake)
    llm.classify_intent("随便一个问题")
    (provider, messages, max_tokens), = calls
    assert provider is llm.INTENT_PROVIDER
    assert provider is not llm.PRIMARY
    assert messages[0] == {"role": "system", "content": llm.INTENT_SYSTEM}
    assert max_tokens == llm.MAX_TOKENS_INTENT

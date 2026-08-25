"""意图分派的测试。

⚠️ **路由错了是静默的** —— 用户会以为系统笨，而不知道是分错了路。
   所以这里的每一条都值得钉死，尤其是「不该命中」的那些负例。

时间解析全部传固定的 today，否则测试会随真实日期漂移 —— 那种失败最难查
（今天绿明天红，而代码一个字没动）。
"""

import datetime

import pytest
from fastapi.testclient import TestClient

from server.main import API, app
from src import llm, router

# 2026-08-21 属于 2026 年 7 月番（Q3）
TODAY = datetime.date(2026, 8, 21)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


# ============================================================
# 时间表达（纯函数）
# ============================================================
@pytest.mark.parametrize(("q", "want"), [
    ("2016年7月番有哪些", (2016, 7)),
    ("2016 年 8 月的番", (2016, 7)),          # 8 月归一化到 7 月番
    ("2016年夏季番", (2016, 7)),
    ("2016年有哪些番", (2016, 7)),            # 只给年份 → 取当前季度的月
    ("十年前的这个季度在播什么", (2016, 7)),
    ("三年前的秋番", (2023, 10)),
    ("去年冬番有哪些", (2025, 1)),
    ("今年春季有什么新番", (2026, 4)),
    ("这个季度在播什么", (2026, 7)),
    ("下一季有什么新番", (2026, 10)),
    ("上一季有哪些番", (2026, 4)),
    ("下下季有什么番", (2027, 1)),             # 跨年
])
def test_parse_cour(q, want):
    assert router.parse_cour(q, TODAY) == want


@pytest.mark.parametrize("q", [
    "2016年的作画怎么样",        # 有年份但没有季度触发词
    "《进击的巨人》的结局是什么",
    "花泽香菜配过哪些角色",
])
def test_parse_cour_rejects(q):
    """⚠️ 必须有季度类触发词才认 —— 否则「2016 年的作画怎么样」会被劫到
    season 分支，而那是个评价类问题，答案不在档期表里。"""
    assert router.parse_cour(q, TODAY) is None


def test_next_quarter_must_be_recognized():
    """🚨 「下一季」不认的话会静默落到「今天所在季度」——
    用户问下季却拿到当季，答非所问且从结果里看不出来。"""
    assert router.parse_cour("下一季有什么新番", TODAY) == (2026, 10)


# ============================================================
# 分派
# ============================================================
@pytest.mark.parametrize(("q", "want"), [
    ("花泽香菜配过哪些角色", "voice"),
    ("2016年7月番有哪些", "season"),
    ("十年前的这个季度在播什么", "season"),
    ("《进击的巨人》的结局是什么", "ask"),
    ("2016年的作画怎么样", "ask"),
    ("给我推荐点像命运石之门的", "ask"),
])
def test_classify(q, want):
    assert router.classify(q, TODAY)[0] == want


def test_classify_returns_reason():
    """route_reason 要能直接显示给用户（「我理解为：…」）。"""
    route, reason = router.classify("2016年7月番有哪些", TODAY)
    assert route == "season"
    assert "2016" in reason and "7" in reason


def test_known_limitation_person_plus_cour():
    """🚨 已知限制固化成测试：「人 + 档期」的复合查询会丢掉人名那一半。

    「花泽香菜今年有哪些新番」→ season，"花泽香菜"被整个忽略。
    这里不是在断言它「对」，而是锁住当前行为 —— 将来真做了
    「按 person_id 过滤档期」，这条测试会红，提醒改文档和 route_reason。
    """
    assert router.classify("花泽香菜今年有哪些新番", TODAY)[0] == "season"


# ============================================================
# 端点
# ============================================================
@pytest.fixture(autouse=True)
def _stub_voice_llm(monkeypatch):
    """⚠️ voice 分支 2026-08-25 起会调一次生成，**本文件不该真的等它**。

    这里测的是「路由选对了没有」，不该为此每条用例都真的打一次外部 API：
    慢（换 8B 之后单次仍要 5~7 秒，换之前是 26~37 秒），而且引入模型行为
    漂移 —— 生成内容变了会让**路由**用例莫名其妙地红。
    与 test_ask_intent.py 同一条纪律：把 llm.* 全部换成替身，只留路由真跑。
    📌 生成本身的 wiring（成功用 LLM 的话 / 失败回落成表格）由
       tests/test_voice.py 覆盖。
    """
    monkeypatch.setattr(
        llm, "voice_answer",
        lambda q, ctx, **kw: ("（测试替身：LLM 组织的回答）", llm.VOICE_PROVIDER))


def test_ask_returns_route(client):
    d = client.post(f"{API}/ask", json={"question": "花泽香菜配过哪些角色",
                                        "top_k": 3}).json()
    assert d["route"] == "voice"
    assert d["route_reason"]
    assert d["voice"] is not None and d["season"] is None
    # ⚠️ 2026-08-25 起 voice 不再是零模型分支（会调一次 llm.voice_answer
    #    把配役表讲成一段话），此处断言随之从 True 改为 False。
    #    **资料仍然是零模型来的**（一条 SQL），改变的只是"讲"的那一步。
    assert d["meta"].get("zero_model") is False
    assert d["meta"].get("llm")
    assert d["meta"].get("voice_order") == "popular"


def test_ask_route_forced(client):
    """按钮路径：裸名字也能查 —— 这正是自动分派覆盖不到的那一类。"""
    d = client.post(f"{API}/ask", json={"question": "花泽香菜",
                                        "route": "voice", "top_k": 3}).json()
    assert d["route"] == "voice"
    assert d["voice"]["name_cn"] == "花泽香菜"


def test_ask_season_branch(client):
    d = client.post(f"{API}/ask", json={"question": "2016年7月番有哪些",
                                        "top_k": 3}).json()
    assert d["route"] == "season"
    assert d["season"]["year"] == 2016 and d["season"]["month"] == 7
    assert d["season"]["total"] > 100          # 实测 134


def test_ask_voice_falls_back(client):
    """⚠️ 空结果**回落而不是 404** —— 用户点错按钮很常见，
    硬失败会让他以为功能坏了。"""
    d = client.post(f"{API}/ask", json={"question": "不存在的声优xyzq",
                                        "route": "voice", "top_k": 3}).json()
    assert d["route"] == "ask"
    assert "声优" in d["route_reason"]


def test_fallback_reason_appears_in_answer(client):
    """🚨 回落原因必须出现在 answer 里，不能只放在 route_reason。

    用户主要看 answer —— 只显示「没认出你问的是哪部作品」对他毫无信息量。
    """
    d = client.post(f"{API}/ask", json={"question": "不存在的声优xyzq",
                                        "route": "voice", "top_k": 3}).json()
    assert "没有找到这个名字的声优" in (d["answer"] or "")


def test_ask_branch_still_works(client):
    """回归：加了分派之后，原本的流程 C 不能受影响。"""
    d = client.post(f"{API}/ask", json={"question": "《命运石之门》的主角是谁",
                                        "top_k": 3}).json()
    assert d["route"] == "ask"
    assert d["voice"] is None and d["season"] is None


# ── 英文时间表达（第四部分「英文提问支持」①）────────────────────

@pytest.mark.parametrize(("q", "want"), [
    # 绝对年月 / 年季
    ("what anime aired in July 2016", (2016, 7)),
    ("what aired in 2016 July", (2016, 7)),
    ("summer 2016 anime", (2016, 7)),
    ("anime from the winter of 2017", (2017, 1)),
    ("what anime aired in fall 2016", (2016, 10)),
    # 裸年份 + 触发词
    ("what anime came out in 2016", (2016, 7)),
    # 相对年份
    ("what anime were people watching ten years ago today", (2016, 7)),
    ("what anime aired 10 years ago", (2016, 7)),
    ("what anime aired a decade ago", (2016, 7)),
    ("anime airing last year", (2025, 7)),
    ("new anime next year", (2027, 7)),
    ("what anime aired in the spring three years ago", (2023, 4)),
    # 相对/当前季度
    ("what is airing this season", (2026, 7)),
    ("what anime is coming out next season", (2026, 10)),
])
def test_parse_cour_english(q, want):
    """英文时间表达同样是**有限模式**，用正则不用模型。

    ⚠️ 交给 LLM 会得到不稳定的日期，而日期错了整个结果就错 ——
       与中文那一侧同一条理由（见 router 模块注释）。
    """
    assert router.parse_cour(q, TODAY) == want


@pytest.mark.parametrize("q", [
    # 🚨 有触发词但**没有时间表达** → 不能劫走，它们是剧情/评价题
    "best anime ending of all time",
    "what is the ending of Steins;Gate",
    "which anime has the best animation",
    # 🚨 "last season" 在英文里几乎总是指「某部作品的上一季」
    "what happened last season in Attack on Titan",
    "will there be a next season of Steins;Gate",
    "what happened this season in Attack on Titan",
    "is the current season of One Piece good",
    # ⚠️ 词边界：may 不能命中 maybe，fall 不能命中 fallen
    "maybe fallen angels is a good anime",
])
def test_parse_cour_english_negatives(q):
    """负例比正例更值钱：路由错了是静默的，用户只会觉得系统笨。"""
    assert router.parse_cour(q, TODAY) is None


def test_english_question_routes_to_season():
    """端到端：上一轮那句中文问题的英文对应版本必须落 season。"""
    route, reason = router.classify(
        "What anime were people watching ten years ago today?", TODAY)
    assert route == "season"
    assert "2016" in reason and "7" in reason


def test_next_season_of_a_show_is_not_a_cour_query():
    """🚨 回归：'next season of X' 问的是续作，不是档期。

    ⚠️ classify() 是纯函数、零 DB，没法靠「句里有作品名」消歧
       ⇒ 唯一能用的确定性信号是**介词**：season 后面跟 of/in/for
         就判为续作义。这条钉住那个护栏，防止有人把否定预查删掉。
    """
    assert router.classify("will there be a next season of Steins;Gate",
                           TODAY)[0] == "ask"

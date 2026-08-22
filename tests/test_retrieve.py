"""检索层的纯函数测试 —— 不连库、不打外部 API。

⚠️ 与 test_api.py 分工不同：那边验 HTTP 外壳，这边验**挑选逻辑本身**。
   相关度地板、保底席位、词边界这三条都是被实测 bug 逼出来的规则，
   而它们的输入都是可以手工构造的 —— 没有理由让它们依赖网络。
"""

import datetime

import pytest

from src import llm, recommend, related
from src.retrieve import (
    GENERIC_NAMES,
    MIN_KEEP,
    MIN_SCORE,
    PIN_RESERVE,
    Chunk,
    _apply_pin_reserve,
    _latin_word_boundary,
    _substrings,
    songs_seat,
)
from src.textproc import norm_name, norm_name_gaps


def mk(cid: int, score: float, *, pinned: bool = False) -> Chunk:
    return Chunk(chunk_id=cid, section=f"s{cid}", text=f"t{cid}", kind="profile",
                 source="bangumi_char", character_id=cid if pinned else None,
                 spoiler_level=0, score=score, pinned=pinned)


# ── 相关度地板（MIN_SCORE）────────────────────────────────────────

def test_floor_drops_diluting_chunks():
    """🚨 回归：低分 chunk 会把答案稀释掉。

    实测「冰之城墙这个漫画的主人公是谁？」的真实分布 —— 前两条里有答案，
    后六条是配角简介：
        全部 8 条        → 「资料中没有提到。」
        砍掉低分的       → 「冰川小雪和雨宫凑是主人公。」
    """
    order = [mk(1, 0.873), mk(2, 0.671), mk(3, 0.060), mk(4, 0.032),
             mk(5, 0.031), mk(6, 0.030), mk(7, 0.025), mk(8, 0.023)]
    out = _apply_pin_reserve(order, final=8)
    assert [c.chunk_id for c in out] == [1, 2, 3], "0.05 以下的必须被砍掉"


def test_floor_keeps_everything_when_all_relevant():
    """高分场景不受影响 —— 地板只砍噪声，不改变正常结果。"""
    order = [mk(i, 0.9 - i * 0.05) for i in range(8)]
    assert len(_apply_pin_reserve(order, final=8)) == 8


def test_floor_never_returns_less_than_min_keep():
    """⚠️ 全都低于地板时也要留 MIN_KEEP 条。

    返回空列表会让调用方走进 NO_CORPUS 分支，**谎称「这部作品没有语料」** ——
    而事实是语料有、只是都不太相关。宁可让 LLM 看着材料说「没提到」。
    """
    order = [mk(i, 0.001) for i in range(6)]
    out = _apply_pin_reserve(order, final=8)
    assert len(out) == MIN_KEEP


# ── 保底席位（PIN_RESERVE）───────────────────────────────────────

def test_pin_survives_the_floor():
    """🚨 回归：pinned 不受相关度地板约束。

    「冈部伦太郎有什么特别的能力」里他本人的 chunk 只有 0.0089 ——
    按地板会被砍掉，而 PIN_RESERVE 存在的全部理由就是保住它
    （G.5g：alias 是确定性的解法，能被分数挤掉就不是确定性的）。
    """
    order = [mk(i, 0.9 - i * 0.1) for i in range(7)] + [mk(99, 0.0089, pinned=True)]
    out = _apply_pin_reserve(order, final=8)
    assert 99 in [c.chunk_id for c in out], "被点名的角色必须在最终结果里"
    assert MIN_SCORE > 0.0089, "这条测试的前提是该分数低于地板"


def test_pin_reserve_is_capped():
    """保底席位有上限，不能让点名把整个上下文占满。"""
    order = [mk(i, 0.001, pinned=True) for i in range(10)] + \
            [mk(50 + i, 0.9) for i in range(5)]
    out = _apply_pin_reserve(order, final=8)
    assert sum(1 for c in out if c.pinned) == PIN_RESERVE


def test_pin_does_not_get_promoted():
    """⚠️ 保底只保「在不在」，不保「排第几」。

    否则不相关的点名 chunk 会伪装成最佳答案 —— 展示顺序仍归 rerank 分。
    """
    order = [mk(1, 0.9), mk(2, 0.8), mk(99, 0.001, pinned=True)]
    out = _apply_pin_reserve(order, final=8)
    assert out[-1].chunk_id == 99


# ── 词边界（_latin_word_boundary）────────────────────────────────

@pytest.mark.parametrize(("raw", "name", "ok"), [
    # ── 假阳性必须继续挡住（原有 5 项，改成从**原始问句**出发）──────
    ("帮我写一个快速排序的 Python 实现", "py", False),   # 英文词中间截出
    ("帮我写一个快速排序的 Python 实现", "hon", False),
    ("怎么把 Excel 表格导出成 CSV", "ex", False),
    ("怎么把excel表格导出", "el", False),                # 中英直接相连，无 gap
    ("eva的结局", "eva", True),                          # 独立出现的真别名
    ("看eva", "eva", True),
    ("蕾姆是谁", "蕾姆", True),                           # 中文不受词边界约束
    # ── 🚨 英文问句回归：修之前这三条全是 False ──────────────────
    ("What happens at the end of Steins;Gate?", "steinsgate", True),
    ("Explain the ending of Attack on Titan", "attackontitan", True),
    ("Who is Mikasa Ackerman?", "mikasaackerman", True),
])
def test_latin_word_boundary(raw, name, ok):
    """🚨 双向回归：既要挡住词中截取，又不能把英文问句全灭。

    **挡住的那一侧**：实测「帮我写一个快速排序的 Python 实现」曾命中
    py/hon，于是一个与动画无关的问题被自信地解析成了某部作品。
    ⚠️ 中文没有词边界，而蕾姆/拉姆/三笠全是 2 字，不能套同一条规则。

    **放行的那一侧**：norm_name 会删掉所有空白，于是英文问句里的标题
    前后都贴着别的单词，与 py/hon 在 norm 串上**长得一模一样**。
    实测修之前 resolve() 对英文 6/6 全灭，且只有整句仅剩标题时才认得出。
    ⇒ 判据必须回到**原文**有没有分隔符，也就是 norm_name_gaps 的 gaps。

    ⚠️ 参数从**原始问句**出发而不是直接给 norm 串 —— 直接给 norm 串就
       绕过了 gaps 的产生过程，这个 bug 恰恰藏在那一步里。
    """
    norm_q, gaps = norm_name_gaps(raw)
    start = norm_q.find(name)
    assert start != -1, f"{name!r} 不在 {norm_q!r} 里，用例本身写错了"
    assert _latin_word_boundary(norm_q, gaps, start, start + len(name), name) is ok


# ── norm_name / norm_name_gaps 防漂移 ───────────────────────────

def test_norm_name_gaps_agrees_with_norm_name():
    """⚠️ 两个函数必须给出同一个归一化串。

    norm_name 定义了 alias 表 26 万行的键，norm_name_gaps 现在是它的
    唯一实现处。哪天有人为了省事把实现复制回 norm_name，这条就红 ——
    与 clients.close_all() 那条「该关谁只能有一个定义处」同构。
    """
    for s in ["Fate/stay night", "ＦＡＴＥ／ＳＴＡＹ　ＮＩＧＨＴ", "蕾姆",
              "三笠·阿克曼", "What happens at the end of Steins;Gate?",
              "", "   ", "！？。", "EVA"]:
        assert norm_name_gaps(s)[0] == norm_name(s)


def test_norm_name_gaps_marks_dropped_separators():
    """gaps[i] = 原文里紧挨 norm[i] 之前丢掉过东西；长度恒为 len+1。"""
    norm_q, gaps = norm_name_gaps("Attack on Titan")
    assert norm_q == "attackontitan"
    assert len(gaps) == len(norm_q) + 1
    assert gaps[norm_q.index("on")] is True        # "on" 前面原来是空格
    assert gaps[norm_q.index("titan")] is True
    assert gaps[1] is False                        # "ttack" 内部没有分隔符


def test_substrings_are_deduped_and_bounded():
    subs = _substrings("蕾姆和拉姆")
    assert "蕾姆" in subs and "拉姆" in subs
    assert len(subs) == len(set(subs)), "不该有重复"
    assert all(2 <= len(s) <= 16 for s in subs)


# ── 关联查询的触发规则（related.wants）───────────────────────────

@pytest.mark.parametrize(("q", "roles", "studio"), [
    ("冰之城墙的作者还画过其他漫画吗？", ["原作"], False),
    ("这部番的导演还做过什么", ["导演"], False),
    ("制作公司是哪家", [], True),
    ("蕾姆和拉姆是什么关系？", [], False),      # 不该触发
    ("进击的巨人讲了什么故事？", [], False),      # 不该触发
])
def test_related_triggers(q, roles, studio):
    """⚠️ 关键词触发，不是意图分类器 —— 第 15 节原则 2：
    能用规则判的别交给模型。误触发的代价只是多跑一条走索引的 SQL。
    """
    assert related.wants(q) == (roles, studio)


def test_related_roles_match_the_database():
    """岗位常量必须与库里实际存在的 role 值一致。

    ⚠️ 凭猜加一个库里没有的 role，查询会静默返回空 —— 不报错，只是没结果。
    """
    assert set(related.ROLE_TRIGGERS) <= set(related.STAFF_ROLES)


# ── 多轮对话的上下文预算（retrieve._trim）────────────────────────

def test_history_is_truncated():
    """🚨 历史不截会把当前这轮的资料挤没。

    I.2 ② 实测：上下文噪声一多，LLM 就从「答对」退化成「资料中没有提到」
    （同一题 8 条→拒答、3 条→答对）。历史是同一种噪声，而且**不受
    MIN_SCORE 那道地板约束** —— 地板管 chunk，管不到历史。
    """
    from src.retrieve import MAX_HISTORY_CHARS, _trim

    long_q, long_a = "问" * 999, "答" * 999
    out = _trim([(long_q, long_a)])
    assert len(out[0][0]) == MAX_HISTORY_CHARS
    assert len(out[0][1]) == MAX_HISTORY_CHARS


def test_trim_handles_empty_answer():
    """短路的那几轮（反问 / 没语料）answer 可能是 None，不能炸。"""
    from src.retrieve import _trim

    assert _trim([("问", None)]) == [("问", "")]


# ── /api/season 的窗口口径（recommend.cour_window）────────────────

def test_cour_window_matches_documented_measurement():
    """第四部分的实测口径：「十年前的这个季度」（2026-08 查）
    = 2016-06-24 ~ 2016-10-01 共 134 部。窗口两端必须逐日吻合，
    差一天口径就变了，而那 134 部是按这个窗口数出来的。
    """
    lo, hi = recommend.cour_window(2016, 8)     # 8 月归到 7 月番
    assert lo == datetime.date(2016, 6, 24)
    assert hi == datetime.date(2016, 10, 1)
    # 同季度内任何月份给出同一个窗口
    assert recommend.cour_window(2016, 7) == (lo, hi)
    assert recommend.cour_window(2016, 9) == (lo, hi)


def test_cour_window_grace_crosses_year_boundary():
    """1 月番的 7 天宽限要跨进上一年的 12 月。"""
    lo, hi = recommend.cour_window(2025, 1)
    assert lo == datetime.date(2024, 12, 25)
    assert hi == datetime.date(2025, 4, 1)


# ── llm.descriptor 的指纹必须覆盖 prompt ──────────────────────────

def test_descriptor_changes_when_answer_prompt_changes(monkeypatch):
    """🚨 回归：改 prompt 指纹一个字符都不变（2026-08-19 之前的实况）。

    后果是第 5 周评测日志声称两批数字同源，而实际 prompt 已经换过了。
    同一条纪律 embed（指纹校验）和 translate_cache（PROMPT_VERSION）
    都做对了，唯独 LLM 这条漏了。
    """
    before = llm.descriptor(llm.PRIMARY)["fingerprint"]
    monkeypatch.setattr(llm, "ANSWER_SYSTEM", llm.ANSWER_SYSTEM + "。")
    assert llm.descriptor(llm.PRIMARY)["fingerprint"] != before


def test_descriptor_changes_when_hyde_prompt_changes(monkeypatch):
    before = llm.descriptor(llm.PRIMARY)["fingerprint"]
    monkeypatch.setattr(llm, "HYDE_SYSTEM", llm.HYDE_SYSTEM + "。")
    assert llm.descriptor(llm.PRIMARY)["fingerprint"] != before


def test_descriptor_is_deterministic():
    """同一份配置两次调用必须给出同一个指纹 —— 它是「同源」的判据本身。"""
    assert llm.descriptor(llm.PRIMARY) == llm.descriptor(llm.PRIMARY)


# ── songs 保底席位（SONGS_SEAT）───────────────────────────────────
#
# 🚨 这一组锁的是第 5 周评测 §4.2 那个 8/8 全失败：songs chunk 每次都被
#    正确召回、排层内第 1，却被 MIN_SCORE 地板砍掉。
#    55 题实测：保底席位 5/7，上下文只 +0.09 条/题；把地板降到 0 只有 4/7
#    却要 +3.44 条/题。**规则完胜调阈值。**

def mk_song(cid: int, score: float) -> Chunk:
    return Chunk(chunk_id=cid, section="主题曲", text=f"OP{cid}", kind="songs",
                 source="moegirl", character_id=None, spoiler_level=0,
                 score=score, pinned=False)


@pytest.mark.parametrize("q, hit", [
    ("《龙王的工作！》的片头曲是什么？", True),
    ("这部动画的片尾曲叫什么", True),
    ("OP 是谁唱的", True),
    ("这部的 op 好听吗", True),          # 小写
    ("ED曲叫什么", True),                # 紧跟中文，仍算边界
    ("主题曲信息", True),
    ("《龙王的工作！》讲了什么故事？", False),   # 不是 OP/ED 题 → 不给席位
    ("三笠是谁", False),
    # 🚨 拉丁词内部的 OP/ED —— 自检发现库里 **75 部**作品名会这样误命中，
    #    其中不乏头部作品。裸子串版本这 5 条全部误触发。
    ("《Fate/stay night [Unlimited Blade Works]》讲了什么故事？", False),
    ("《SPEED GRAPHER》的结局是什么？", False),
    ("《TOP をねらえ!》讲了什么故事？", False),
    ("《pop子和pipi美的日常》讲了什么", False),
    ("《恋爱FLOPS》讲了什么故事？", False),
])
def test_songs_seat_only_fires_on_op_ed_questions(q, hit):
    pool = [mk(1, 0.9), mk_song(2, 0.003)]
    assert (songs_seat(q, pool) == 2) is hit


def test_songs_seat_survives_the_floor():
    """🚨 回归：∀高达 0.0032 / 跟班×服务 0.0034 —— 全在 0.05 地板之下。"""
    order = [mk(1, 0.543), mk(2, 0.4), mk(3, 0.3), mk(4, 0.2),
             mk(5, 0.1), mk(6, 0.09), mk(7, 0.08), mk(8, 0.07),
             mk_song(9, 0.0032)]
    plain = _apply_pin_reserve(order, final=8)
    assert 9 not in [c.chunk_id for c in plain], "没有席位时它本来就该被砍掉"

    out = _apply_pin_reserve(order, final=8, seat=9)
    assert 9 in [c.chunk_id for c in out], "给了席位就必须保住"
    assert len(out) == 8


def test_songs_seat_must_reserve_not_merely_exempt():
    """🚨 **这条锁的是「占座」与「豁免地板」的区别 —— 只豁免会失败。**

    构造：8 条都在地板之上，songs 分数最低。
    若实现成「豁免地板」，songs 仍要和它们按分数序竞争 `[:room]`，
    排在第 9 位被截掉；只有**占座**才进得来。
    ⚠️ 这正是 I.2 ① 那条教训（PIN_RESERVE 保「在不在」）的同一形态，
       没有这条测试，将来有人"简化"成豁免版不会有任何红灯。
    """
    order = [mk(i, 0.9 - i * 0.05) for i in range(1, 9)] + [mk_song(99, 0.001)]
    assert all(c.score >= MIN_SCORE for c in order[:8])
    out = _apply_pin_reserve(order, final=8, seat=99)
    assert 99 in [c.chunk_id for c in out], "占座必须挤掉一条普通 chunk"
    assert len(out) == 8


def test_songs_seat_does_not_eat_pin_reserve():
    """席位独立于 PIN_RESERVE：4 条 pinned 占满时 songs 仍进得来。

    ⚠️ 这条是**按构造定的，不是实测定的** —— 55 题题库里两者只在《∀高达》
       同时出现且 pinned 只有 2 条，没撞上。留测试是为了把这个选择钉死：
       "保底"一旦有条件就不再是保底。
    """
    order = ([mk(i, 0.9) for i in range(1, 5)]          # 4 条高分普通
             + [mk(10 + i, 0.001, pinned=True) for i in range(PIN_RESERVE)]
             + [mk_song(99, 0.001)])
    out = _apply_pin_reserve(order, final=8, seat=99)
    ids = [c.chunk_id for c in out]
    assert 99 in ids, "pinned 占满 4 席时 songs 席位仍须生效"
    assert sum(1 for c in out if c.pinned) == PIN_RESERVE


def test_songs_seat_absent_is_a_noop():
    """池子里没有 songs chunk（大闹天宫那类）→ 行为与今天完全一致。"""
    order = [mk(1, 0.873), mk(2, 0.671), mk(3, 0.060), mk(4, 0.032)]
    assert songs_seat("《大闹天宫》的片头曲是什么？", order) is None
    assert ([c.chunk_id for c in _apply_pin_reserve(order, final=8, seat=None)]
            == [c.chunk_id for c in _apply_pin_reserve(order, final=8)])


# ============================================================
# 泛称不参与作用域投票（2026-08-22）
# ============================================================
def test_generic_names_are_semantic_not_frequency_based():
    """⚠️ 泛称只能靠**语义列举**，不能按「撞几部作品」自动筛。

    实测撞得最多的全是真名字：爱丽丝 26 部 · マリー 25 · 莉莉 23 · 田中 16，
    而真正的泛称「女主角」只撞 1 部（《宝可梦进化》）。
    这条测试防的是有人把 GENERIC_NAMES 改成按 df 自动生成 —— 那会把
    爱丽丝这类真名字全部误杀，而漏掉「女主角」。
    行为层面的验证在 tests/test_api.py（需要连库）。
    """
    assert "爱丽丝" not in GENERIC_NAMES
    assert "田中" not in GENERIC_NAMES
    assert {"主人公", "女主角", "男主角"} <= GENERIC_NAMES

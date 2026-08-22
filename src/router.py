"""意图分派 —— 单一输入框背后的那个分岔口。

第四部分「单一入口 vs 功能按钮」定的架构：**按钮不是独立功能，而是强制指定
route 的参数**，A（按钮）与 B（单一入口）因此不是二选一，B 涵盖 A。

⚠️ **这里做的是「意图路由」，不是「意图分类器」。** 第 15 节原则 2 反对的是
   让模型去判；而这里四条分支的信号全部是**已有的确定性信号**：
     resolve()      认出具体作品/角色  → ask（流程 C，内含 related 补充上下文）
     voice.wants()  声优触发词          → voice
     时间表达正则    季度/年份/相对时间   → season
     以上都不命中                       → ask 兜底
   没有一处需要模型来判意图，也就没有「分类器不稳定」这回事。

⚠️ **时间表达用正则不用模型。** 「十年前的这个季度」「2016 年 7 月番」
   「今年春季」是**有限的模式**，交给 LLM 反而会得到不稳定的日期，
   而日期错了整个结果就错了。

📌 **路由准确率实测（2026-08-21）**：
     60 题评测集（格式规整）      判对 56/56（错的 4 道全是 season，当时未实现）
     口语 / 省略型输入            判错 5/10
   ⇒ **按钮救的不是「歧义」而是「用户不肯把话说完整」**：
     「花泽香菜」「钉宫理惠 龙与虎」「三笠的声优」这类，规则想覆盖就得放宽，
     一放宽又会把「这部动画的声优阵容怎么样」这种闲聊吞进去。
     有了按钮，**自动分派保持保守、按钮路径可以完全不保守**（见 relax_voice）。

⚠️ **响应必须回传实际走的 route。** 路由错了是**静默**的 —— 用户会以为系统笨，
   而不知道是分错了路。「我理解为：查询 2016 年夏季新番 ▾」比静默猜测好得多，
   与 G.4 反问同一条「代价不对称」逻辑：多一次点击很便宜，自信地答错很贵。
"""

from __future__ import annotations

import datetime
import re
from typing import Literal

from src import voice

Route = Literal["ask", "voice", "season"]

# ── 季度 ────────────────────────────────────────────────────
# 月份 → 归一化到的季度起月。传 8 月自动归到 7 月番，与 recommend.cour_window 一致。
_COUR = {1: 1, 2: 1, 3: 1, 4: 4, 5: 4, 6: 4, 7: 7, 8: 7, 9: 7, 10: 10, 11: 10, 12: 10}
_SEASON_WORD = {"春": 4, "夏": 7, "秋": 10, "冬": 1}

# 「2016年7月番」「2016 年 7 月的番」
_Y_M = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")
# 「2016年春季」「2016 年夏番」
_Y_S = re.compile(r"(\d{4})\s*年\s*(?:的\s*)?([春夏秋冬])")
# 「2016年」单独出现（无月份/季节）
_Y = re.compile(r"(\d{4})\s*年")
# 「十年前」「10 年前」「三年前」
_AGO = re.compile(r"([0-9零一二两三四五六七八九十百]+)\s*年前")
# 「今年春季」「去年冬番」
_REL_Y = {"今年": 0, "本年": 0, "去年": -1, "前年": -2, "明年": 1}
# 相对季度。⚠️ **必须支持「下一季」**：不认的话它会落到「今天所在季度」，
#    用户问下季却拿到当季 —— 答非所问，而且从结果里看不出来。
#    认了之后会命中 season 分支的空结果分支，那里有正确的话术。
_REL_COUR = {"下下季": 2, "下一季": 1, "下个季度": 1, "下一个季度": 1, "下季": 1,
             "上一季": -1, "上个季度": -1, "上一个季度": -1, "上季": -1}
_COUR_MONTHS = (1, 4, 7, 10)
_CN_NUM = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 只有这些词出现才考虑走 season —— 否则「2016 年的作画怎么样」也会被劫走。
_SEASON_TRIGGERS = ("番", "季度", "季番", "播出", "开播", "上映", "这一季",
                    "当季", "新番", "在播", "有哪些", "有什么")


def _cn_int(s: str) -> int | None:
    """把「十」「十年」「二十」这类中文数字转成 int。只处理 100 以内。"""
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN_NUM for c in s):
        return None
    if s == "十":
        return 10
    if s.startswith("十"):                      # 十二
        return 10 + _CN_NUM[s[1]]
    if len(s) == 3 and s[1] == "十":            # 二十三
        return _CN_NUM[s[0]] * 10 + _CN_NUM[s[2]]
    if len(s) == 2 and s[1] == "十":            # 二十
        return _CN_NUM[s[0]] * 10
    if len(s) == 1:
        return _CN_NUM[s]
    return None


def parse_cour(question: str, today: datetime.date | None = None
               ) -> tuple[int, int] | None:
    """从问句里解析出 (year, month)，month 已归一化到 1/4/7/10。认不出返回 None。

    ⚠️ **必须有季度类触发词才认**，否则「2016 年的作画怎么样」这种也会被
       劫到 season 分支去 —— 那是个剧情/评价类问题，答案不在档期表里。
    """
    # ⚠️ 用 UTC，与 GET /api/season 的缺省口径一致 —— 两处对「今天是哪个季度」
    #    的理解必须相同，否则同一句「这个季度」在两条路径上会指向不同的档期。
    today = today or datetime.datetime.now(datetime.UTC).date()
    if not any(k in question for k in _SEASON_TRIGGERS):
        return None

    m = _Y_M.search(question)
    if m:
        mon = int(m.group(2))
        if 1 <= mon <= 12:
            return int(m.group(1)), _COUR[mon]

    m = _Y_S.search(question)
    if m:
        return int(m.group(1)), _SEASON_WORD[m.group(2)]

    # 相对年份：「十年前的这个季度」「去年春季」
    year = None
    m = _AGO.search(question)
    if m:
        n = _cn_int(m.group(1))
        if n is not None:
            year = today.year - n
    if year is None:
        for word, delta in _REL_Y.items():
            if word in question:
                year = today.year + delta
                break

    if year is not None:
        for word, mon in _SEASON_WORD.items():
            if word in question:
                return year, mon
        return year, _COUR[today.month]        # 「十年前的这个季度」

    # 只写了年份 + 季度触发词：「2016 年有哪些番」→ 当季对应的月
    m = _Y.search(question)
    if m:
        return int(m.group(1)), _COUR[today.month]

    # 相对季度：「下一季」「上个季度」。⚠️ 要在「这个季度」之前判。
    for word, n in _REL_COUR.items():
        if word in question:
            idx = _COUR_MONTHS.index(_COUR[today.month]) + n
            return today.year + idx // 4, _COUR_MONTHS[idx % 4]

    # 「这个季度」「当季新番」
    if any(k in question for k in ("这个季度", "本季", "当季", "这一季")):
        return today.year, _COUR[today.month]
    return None


def relax_voice(question: str) -> bool:
    """按钮路径下的宽松判据：只要问句里像是个人名就行。

    ⚠️ **这是按钮存在的主要架构收益，不是可有可无的分支。**
       自动分派必须保守（误判是静默的），所以 voice.wants() 要求触发词组合；
       而用户点了「声优」按钮就是显式指令，此时**不需要任何触发词** ——
       「花泽香菜」「钉宫理惠 龙与虎」「三笠的声优」这类立刻可用。
       实测这正是规则判错的那 5/10 输入。
    """
    return len(question.strip()) >= 2


def classify(question: str, today: datetime.date | None = None
             ) -> tuple[Route, str]:
    """自动分派。返回 (route, 给用户看的一句说明)。

    ⚠️ 顺序有意义：voice 判在 season 之前。

    🚨 **已知限制：「人 + 档期」的复合查询处理不了**，实测
       「花泽香菜今年有哪些新番」→ season，**"花泽香菜"这个限定被整个忽略**，
       返回的是今年全部新番。（它没走 voice 不是因为顺序，是因为
       voice.wants() 里没有匹配的触发词 —— 换句话说调顺序也救不了。）
       ⇒ 要修得让 season 支持按 person_id 过滤，那是 voice_role JOIN
         anime_profile 再加档期窗口，一条 SQL，但属于新功能不是路由的事。
       ⚠️ 在那之前，**route 回显是唯一的补救** —— 用户看到「按档期浏览
         2026 年 7 月番」就知道系统没理解"花泽香菜"，可以改用按钮。
    """
    if voice.wants(question) == "person":
        return "voice", "按声优配役查询"
    cour = parse_cour(question, today)
    if cour:
        return "season", f"按档期浏览 {cour[0]} 年 {cour[1]} 月番"
    return "ask", "按剧情问答处理"

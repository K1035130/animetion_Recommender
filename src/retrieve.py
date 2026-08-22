"""检索层 —— 流程 C（剧情问答）的四步管道。阶段 05，CLAUDE.md G 节。

    ① resolve   名字 → alias → character_id / series_root      G.4 · G.5g
    ② recall    作用域内向量 top-50（角色 40 + 系列 10 保底）    G.6
    ③ rerank    bge-reranker-v2-m3 → 前 8                      G.6
    ④ answer    llm.answer()，chunks 为空则短路不调用            G.4 状态③

⚠️ **① 排在最前面是实测逼出来的，不是随意排的。** 四道问答测试里三道败在
   「角色本人的 chunk 没被召回」，根因是 **93.3% 的角色 chunk 正文里没有
   角色自己的名字**（F.7b ①）—— dump 的简介写的是「艾伦的母亲」，主语是别人，
   而名字只在 section 列、section 不参与 embedding。
   实测【蕾姆】在纯向量里排第 38 位，k=20/30 都够不着。
   ⇒ 加大 k 只是碰运气，确定性的解法是按 character_id 直取。

⚠️ **但 ① 是加速器不是硬前提。** 自检实测 alias 只收录官方书写形态
   （char_name / char_name_cn / char_alias），**没有粉丝简称**：

       ✅ 蒙奇·D·路飞        ❌ 路飞
       ✅ 三笠·阿克曼        ❌ 米卡莎
       ✅ 冈部伦太郎         ❌ 凤凰院凶真

   而用户打的就是「路飞」。所以 ① 命中不了时必须干净地退回 ②③，
   绝不能让整条链路失败 —— 描述性查询（「三笠对艾伦是什么感情」）
   本来也不该走 ①。**两者互补，不是主备。**
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import psycopg

from src import embed, llm, related, textproc
from src import rerank as rr

# ── 参数（G.6 实测定案，改之前先重跑那轮召回宽度扫描）─────────────
# 目标 chunk 在纯向量里的真实排名：top-4 / 13 / 9 / 21 / 38
#   k=20 两道够不着 · k=30 一道够不着 · k=50 全中 · k=80 无额外收益
QUOTA_CHAR = 40      # 角色层配额
QUOTA_SERIES = 10    # 系列层保底（prose）
QUOTA_SONGS = 3      # 歌曲层保底 —— 见下
FINAL = 8            # 交给 LLM 的条数

# ── 相关度地板 ───────────────────────────────────────────────────
# 🚨 **低分 chunk 会把答案稀释掉，这是实测出来的，不是理论担忧。**
#    「冰之城墙这个漫画的主人公是谁？」rerank 分布出现明显断层：
#        0.873  0.671 | 0.060  0.032  0.031  0.030  0.025  0.023
#    前两条里有答案（前言 + 剧情简介点了四个中心人物），后六条是配角简介。
#        全部 8 条        → 「资料中没有提到。」          ❌
#        只留 >0.1 的两条 → 「冰川小雪和雨宫凑是主人公。」  ✅
#    ⚠️ 我先试的是改 prompt（允许从资料推断），**两版都拒答，没用** ——
#       所以这不是 prompt 能解决的，是上下文里噪声太多。
#
# ⚠️ **rerank 的分数此前只被用来排序，绝对值被扔掉了。** G.6 那句
#    「最终排序全权交给 rerank」只说对了一半：它同时也在说「这条有多相关」，
#    而把 0.02 的东西塞进 prompt 是在花钱买噪声。
#
# ⚠️ **0.05 是按 10 条查询的分布定的，样本偏小 —— 别把它当定论。**
#    实测最低的一条是「三笠·阿克曼是谁」，top1 只有 0.147。
#    CLAUDE.md 的 EMB_TOL 教训就是拿 12 组小样本定阈值、扩样后余量只剩
#    1.3 倍 ⇒ 做成参数，真正定值等第 5 周有真实查询分布再扫。
MIN_SCORE = 0.05
# 就算全都低于地板也至少留这么多 —— 宁可让 LLM 看着材料说「没提到」，
# 也不要因为空列表走进 NO_CORPUS 分支（那会谎称「这部作品没有语料」）。
MIN_KEEP = 2

# ⚠️ 召回总量是三层之和，**不要再写一个独立的 RECALL_TOTAL 常量**。
#    加 QUOTA_SONGS 那次就漏改了它（还停在 50，而实际是 53），
#    而它只在自己的定义处出现过 —— 不报错，只是悄悄给你一个错的数，
#    与 E.7c ③「统计输出把条目总数写死」同族。
RECALL_TOTAL = QUOTA_CHAR + QUOTA_SERIES + QUOTA_SONGS

# 🚨 **QUOTA_SONGS 是实测逼出来的，别把它并回 QUOTA_SERIES。**
#    第一版只分两层，「命运石之门的片头曲是什么」一条 songs 都召不回：
#
#        #1..#10  全是 prose（前言 / 登场人物 / 小说版 / 广播剧…）← 配额边界
#        #12 #13 #15 #18  SONGS  ← 全被切掉，而 #13 里就写着 OP
#
#    根因：songs chunk 是「OP“スカイクラッドの観測者”作词：志仓千代丸」这种
#    条目式文本，与「片头曲是什么」这句话的**语义**距离并不近 —— E.4 早就
#    写过「歌名是关键词查找，走 BM25 更准」，这里是同一个现象在向量侧的表现。
#    ⚠️ 而 OP/ED 是 E.4 列的四类常见问题之一，**当初漏掉它已经是犯过一次的
#       判断错误**（"最初只按剧情裁剪，Kevin 问的 OP/ED 一条都答不了"）。
#
#    给它一个独立地板是最小修法：多 3 条进 rerank，不相关时 rerank 会把它们
#    压下去（G.6 实测 rerank 不受条数比例影响）。
#    ⬜ 真正的解法仍是 G.1 那条 `search_tsv @@ query` 的 BM25 腿 —— 见 CLAUDE.md。

# ⚠️ 配额只作用在**召回**阶段，最终排序全权交给 rerank。
#    G.6 实测：进击的巨人那题有无配额最终前 8 完全相同 —— rerank 按相关性判，
#    不受条数比例影响，自己就解决了 G.3 担心的「角色层挤掉系列层」。
#    在最终阶段硬分名额反而会把「出版信息」这类低相关的系列层 chunk 顶进来。
#    保底的价值只在于：万一系列层一条都没进候选池，rerank 就无从挽救。
#
# ⚠️ 自检实测 3,621/6,131（59%）的作用域**系列层为 0 条**（萌娘覆盖率只有 40%），
#    所以代码不能假设两层都有内容。SQL 的 row_number 分区天然处理这种情况。

# 名字扫描的子串长度上下界。
#   下界 2：单字别名（自检见到 595 行 1–2 字别名）误命中率太高，
#           「拉」「姆」这类会把整句话炸成一堆假实体。
#   上界 16：库里最长的角色 norm_name 也在这个量级，再长纯属浪费。
MENTION_MIN = 2
MENTION_MAX = 16

# ① 点名角色在最终结果里的保底席位。
#
# 🚨 **这个常量是实测逼出来的，不是保险起见。** 第一版让 rerank 全权决定前 8，
#    结果「冈部伦太郎有什么特别的能力」把冈部本人的 chunk 挤出了前 8，
#    而排第 1 的是**菲利斯·喵喵**（"自称只要注视对方眼睛就知道内心想法"）——
#    正是 G.5f 里让 Hunyuan-A13B 张冠李戴的那一条。
#    根因：冈部本人的简介里没有「能力」二字，菲利斯的有，cross-encoder 按
#    字面相关性判就选了后者。
#
# ⚠️ 这直接推翻了「让 rerank 全权决定最终排序」那条（G.6 末尾）——
#    那条结论是在**没有 ① 直取**的实验里得到的，前提不含点名场景。
#    G.5g 说 alias 路径是「确定性的解法」，**能被 rerank 挤掉就不是确定性的**。
#    ⇒ 保底只保「在不在」，**不保排第几** —— 排序仍然全归 rerank。
# 🚨 **泛称不参与作用域投票。** 它们被收进了 alias（dump 里确实有角色叫这个），
#    但对「是哪部作品」零指代力，却会和真角色名平票、把问句拖进反问。
#    实测：「女主角司波深雪的最新进度」→
#      司波深雪 → 魔法科(1票)  vs  女主角 → 宝可梦进化(1票)  → 并列 → 反问，
#    而用户明明已经点了具体角色名。
#    📌 这是 I.1 那条「主角重名 6.4%（アリス×9、主人公×9）」的活体样本。
# ⚠️ **只能靠语义列举，不能按「撞几部」筛** —— 实测撞得最多的全是真名字
#    （爱丽丝 26 部 · マリー 25 · 莉莉 23），而「女主角」只撞 1 部。
# ⚠️ **不删 alias 行**：那是数据，删了某些角色就再也检索不到（有作品的角色
#    本名就叫「主人公」）。这里只在**投票**这一步忽略它们 —— 全是泛称时
#    仍然退回用它们，否则「这部动画的主人公是谁」会变成 UNKNOWN。
GENERIC_NAMES = frozenset({
    "主人公", "主角", "女主角", "男主角", "主人翁", "男主", "女主",
    "旁白", "叙述者", "玩家", "プレイヤー", "ナレーター", "ナレーション",
    "无名", "名前なし", "村民", "路人", "群众", "众人",
})

PIN_RESERVE = 4

# ── OP/ED 题的 songs 保底席位 ────────────────────────────────────
#
# 🚨 **第 5 周评测 §4.2：「片头片尾」8/8 全失败，100% 是 MIN_SCORE 造成的。**
#    songs chunk **每次都被正确召回、且排层内第 1**（QUOTA_SONGS 工作正常），
#    但 rerank 分只有 0.003–0.028，被 0.05 的地板全部砍掉。
#
# 📌 **根因不是阈值定高了，是绝对阈值在这类查询上根本不是对的判据。**
#    songs chunk 是条目式文本（`コレカラ 歌：Machico 作词：森由里子…`），
#    与「片头曲是什么」这句自然语言的 cross-encoder 相关性天然就低 ——
#    E.4 早写过「歌名是关键词查找，走 BM25 更准」，这是同一现象在 rerank 侧的形态。
#
# 🚨 **而且 1e-3 量级的地板在物理上就调不稳**：
#    `scripts/eval_min_score.py --probe-rerank` 实测 rerank 分数噪声
#    **5e-4 ~ 8e-4**（批次组成 + 服务端连续批处理），
#    最低那条 songs chunk 才 0.0032 —— 噪声占它的 25%。
#
# ✅ **55 题实测对比（scripts/eval_min_score.py sweep）**：
#
#        判据                      songs 命中   上下文新增/题
#        绝对 0.05（原）              0/7          —
#        绝对 0.0                     4/7        +3.44
#        相对 top1×0.02               4/7        +2.02
#        **保底席位（本方案）**       **5/7**    **+0.09**
#
#    ⇒ 规则完胜调阈值：救回更多，代价小 38 倍。**这正是第 15 节原则 2
#      「确定性门控 > 调参」的一个带数字的实例。**
#    ⚠️ 剩下 2 题（大闹天宫 / 西游记之大圣归来）**池子里根本没有 songs chunk**，
#       是语料覆盖问题，地板怎么改都救不回 ⇒ 在能修的 5 题上是 5/5。
#
# ⚠️ **必须是「占座」不是「豁免地板」** —— 这是 I.2 ① 那条教训的原样复发：
#    `kept = [c for c in others if keep(c)][:room]` 按分数降序取前缀，
#    只放行不占座的话，一条分数极低的 chunk 仍会被 `[:room]` 截掉。
#
# ⚠️ **席位独立于 PIN_RESERVE，不共用那 4 席。**
#    实测题库里两者只在《∀高达》一题同时出现，且 pinned 只有 2 条、没撞上 ——
#    **所以这条是按构造选的，不是实测选的**：pinned 占满 4 席时共用会让
#    songs 席位失效，而"保底"一旦有条件就不再是保底。代价至多多 1 条上下文。
# 🚨 **OP / ED 必须加拉丁词边界，裸子串会大面积误命中。**
#    自检实测：库里 **75 部**作品名含裸 OP/ED，其中不乏头部作品 ——
#        Fate/stay night [Unlimit**ed** Blade Works] · **pop**子和pipi美的日常
#        SPE**ED** GRAPHER · **TOP** をねらえ! · 恋爱FL**OP**S
#    问这些作品**任何**问题都会触发 songs 席位，把一条无关 chunk 塞进上下文 ——
#    正是 I.2 ② 那个"低分 chunk 稀释上下文把 LLM 逼成拒答"的成因。
#    ⚠️ 与 `_latin_word_boundary()` 是同一类问题、同一条修法
#       （那个是给 alias 扫描用的，这里模式固定，用 lookaround 更直接）。
#    ⚠️ 中文关键词不需要边界：「片头」不会出现在别的词里面。
SONGS_QUERY = re.compile(
    r"片头|片尾|主题曲|插曲|(?<![A-Za-z])(?:OP|ED)(?![A-Za-z])", re.IGNORECASE)
SONGS_SEAT = 1

# ① 直取的上限。
#
# 🚨 **实测可达，不是理论边界。** 构造一条 235 字、点到 57 个航海王角色的问句，
#    pinned 57 + 召回 53 = **110 条**，超过 rerank 的 MAX_DOCS=100
#    → 抛 RerankError → 静默降级成纯向量序，
#    而那条错误信息说的是「召回宽度失控」，**把责任推给了召回，归因是错的**。
#    航海王一部作品就有 924 个角色，长问句撞上几十个并不需要恶意构造。
#
# ⚠️ 取 40 而不是 47（=100−53）是留余量：将来调大任何一个 QUOTA 都不会
#    悄悄把这条约束顶穿。⇒ 最坏 40+53=93 < 100。
MAX_PINNED = 40

# ── 请求路径的延迟预算 ───────────────────────────────────────────
# 🚨 **src/embed.py 的模块级默认值（MAX_RETRIES=5 × TIMEOUT=60）不能用在这里。**
#    那套是给 build_embeddings.py 那种跑一小时的离线任务标定的 ——
#    那里多等五分钟远比重跑一小时便宜。请求路径的取舍完全相反：用户在等。
#
#    阶段 05 实测撞到过一次：服务端卡顿 → 单条查询端到端 **883 秒**，
#    而正常只要 1.58 秒；期间一直握着 Neon 连接，连接随后被 serverless
#    回收，报 `server closed the connection unexpectedly`
#    —— 正是 A.7「长耗时任务不能跨阶段持有 Neon 连接」那条的复发，
#    只不过这次的「长耗时」不是设计出来的，是重试策略放大出来的。
#
# ⚠️ embedding **不能降级**（A.8：换模型只会返回排好序的噪声），所以这里
#    失败就是整条向量检索失败，正确的兜底是退回纯 BM25 —— 那是调用方的事。
#    本层能做的只是**尽早失败**，别让用户干等。最坏 ≈ 12×2 + 退避 ≈ 25 秒。
REQUEST_EMBED_RETRIES = 2
REQUEST_EMBED_TIMEOUT = 12.0

# ── 多轮对话的上下文预算 ─────────────────────────────────────────
# 反问（G.4 状态②）只有在用户**能把答案送回来**时才有意义，否则那句
# 「你是指哪一部？」就是死路。⇒ 支持两种续问，且**两种的性质完全不同**：
#
#   ① 结构化续问：客户端把选中的 series_root 传回来  → 零模型、确定性
#   ② 自由多轮  ：把最近几轮 Q&A 传回来             → 交给 LLM 消解指代
#
# ⚠️ **能用 ① 就别用 ②**（第 15 节原则 2：能用规则判的别交给模型）。
#    消歧回合的答案是一个 id，让 LLM 从「我要第二个」里猜 id 是白白引入不确定性。
#
# 🚨 **上下文长度必须有硬上限，理由是实测过的**：I.2 ② 发现低分 chunk 会
#    稀释上下文、把 LLM 逼成拒答（同一题 8 条→拒答、3 条→答对）。
#    历史消息是**同一种噪声**，而且它不像 chunk 那样受 MIN_SCORE 约束。
#    ⇒ 只带最近几轮、每轮再截断，宁可少带也不要把当前这轮的资料挤没了。
MAX_HISTORY_TURNS = 3
MAX_HISTORY_CHARS = 300      # 每条消息的截断长度


class State(str, Enum):
    """G.4 的四种解析状态。**返回状态而不是直接返回 id**，
    因为四种状态的正确行为完全不同，而它们在用户眼里症状相同。"""

    OK = "ok"                 # ① 唯一 + 有语料 → 直接检索
    AMBIGUOUS = "ambiguous"   # ② 多个候选 → 反问，不要猜
    NO_CORPUS = "no_corpus"   # ③ 认出来但没语料 → 短路，**不调 LLM**
    UNKNOWN = "unknown"       # ④ 完全没认出 → 说找不到


@dataclass(frozen=True)
class Mention:
    """问句里被识别出的一个实体。"""

    text: str                 # 命中的（归一化后）子串
    entity_type: str          # 'subject' | 'character'
    character_id: int | None
    series_root: int
    title: str                # 该 series_root 的作品名，反问时展示用


@dataclass
class Resolution:
    state: State
    series_root: int | None = None
    title: str | None = None
    character_ids: list[int] = field(default_factory=list)
    candidates: list[Mention] = field(default_factory=list)   # 状态② 的候选
    mentions: list[Mention] = field(default_factory=list)


@dataclass
class Chunk:
    chunk_id: int
    section: str | None
    text: str
    kind: str
    source: str
    character_id: int | None
    spoiler_level: int
    score: float | None = None      # rerank 相关度；None = 未经 rerank
    pinned: bool = False            # 来自 ① 直取而非向量召回

    def as_llm_pair(self) -> tuple[str | None, str]:
        return (self.section, self.text)


@dataclass
class Answer:
    state: State
    text: str | None
    chunks: list[Chunk]
    resolution: Resolution
    meta: dict


# ============================================================
# ① 解析：名字 → character_id / series_root
# ============================================================

def _substrings(norm_q: str) -> list[str]:
    """问句归一化后的所有子串（长度 MENTION_MIN..MENTION_MAX）。

    ⚠️ **为什么是暴力枚举子串而不是分词。** jieba 用的是 tag 词表
       （308 个题材词，server/main.py 启动时校验指纹），**里面没有角色名** ——
       拿它切「蕾姆和拉姆是什么关系」只会把人名切碎。而给 jieba 再加一份
       19.6 万行的角色词典，就等于引入第二份必须与库同步的词典，
       正是 A.2「jieba 词典必须锁死」那条纪律最怕的东西。

       子串枚举没有这个问题：**它查的就是 alias 表本身，天然不会漂移。**
       问句一般 <30 字 → 几百个子串 → 一条 = ANY(...) 查询解决。
    """
    n = len(norm_q)
    seen: set[str] = set()
    out: list[str] = []
    for i in range(n):
        for length in range(MENTION_MIN, min(MENTION_MAX, n - i) + 1):
            s = norm_q[i:i + length]
            if s not in seen:
                seen.add(s)
                out.append(s)
    return out


def _latin_word_boundary(norm_q: str, start: int, end: int, name: str) -> bool:
    """纯拉丁/数字的别名必须落在词边界上。中日文名不受此限。

    🚨 **实测抓到的假阳性，不是防御性编程。** 10 条与动画无关的日常问句里
       有 3 条被误判，其中两条就栽在这里：

           「帮我写一个快速排序的 Python 实现」 → py / hon
           「怎么把 Excel 表格导出成 CSV」      → el / ex

       这些都是从更长的英文词**中间**截出来的两字符片段，恰好撞上了
       196,669 行别名里的短别名。加词边界后它们全部被挡掉，
       而独立出现的真别名（EVA / CLANNAD / Fate）不受影响 ——
       因为那时它们两侧本来就不是字母数字。

    ⚠️ **中文不能用同一条规则**：中文没有词边界，而「蕾姆」「拉姆」「三笠」
       全是 2 字，按边界判会把真角色名一起杀掉。
    """
    if not name.isascii() or not name.isalnum():
        return True
    before_ok = start == 0 or not (norm_q[start - 1].isascii()
                                   and norm_q[start - 1].isalnum())
    after_ok = end >= len(norm_q) or not (norm_q[end].isascii()
                                          and norm_q[end].isalnum())
    return before_ok and after_ok


def find_mentions(conn: psycopg.Connection, question: str) -> list[Mention]:
    """扫出问句里所有能对上 alias 的实体，**长的优先、不重叠**。

    ⚠️ 归一化必须用 textproc.norm_name()，**不能用 .lower()**。
       库里存的是 NFKC + casefold + 去掉所有标点的形态：
       「三笠·阿克曼」→「三笠阿克曼」。角色名里的间隔号极常见
       （蒙奇·D·路飞 / 三笠·阿克曼），用错函数会**静默漏掉一大批**
       —— 与「查询词必须走同一套 jieba」是同一条纪律，写自检脚本时就踩过。
    """
    norm_q = textproc.norm_name(question)
    if len(norm_q) < MENTION_MIN:
        return []

    cands = _substrings(norm_q)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.norm_name, a.entity_type, a.character_id,
                   p.series_root, coalesce(p.name_cn, p.name)
              FROM alias a
              JOIN anime_profile p
                ON p.subject_id = coalesce(a.parent_subject_id, a.subject_id)
             WHERE a.norm_name = ANY(%s)
             GROUP BY a.norm_name, a.entity_type, a.character_id,
                      p.series_root, coalesce(p.name_cn, p.name)
        """, (cands,))
        rows = cur.fetchall()

    if not rows:
        return []

    by_norm: dict[str, list[tuple]] = {}
    for norm_name, entity_type, cid, root, title in rows:
        by_norm.setdefault(norm_name, []).append((entity_type, cid, root, title))

    # 按「命中的子串在问句里的位置 + 长度」做最长非重叠匹配。
    # ⚠️ 不做这一步的话，除了真实体还会命中一堆两字噪声
    #    （196,669 行别名里 1–2 字的不少），把问句炸成假实体。
    spans: list[tuple[int, int, str]] = []
    for norm_name in by_norm:
        start = norm_q.find(norm_name)
        while start != -1:
            spans.append((start, start + len(norm_name), norm_name))
            start = norm_q.find(norm_name, start + 1)
    spans.sort(key=lambda s: (-(s[1] - s[0]), s[0]))    # 长的优先，同长靠前优先

    taken: list[tuple[int, int]] = []
    out: list[Mention] = []
    for start, end, norm_name in spans:
        if not _latin_word_boundary(norm_q, start, end, norm_name):
            continue
        if any(not (end <= ts or start >= te) for ts, te in taken):
            continue                      # 与已选片段重叠 → 跳过（长的已经赢了）
        taken.append((start, end))
        for entity_type, cid, root, title in by_norm[norm_name]:
            out.append(Mention(text=norm_name, entity_type=entity_type,
                               character_id=cid, series_root=root, title=title))
    return out


def resolve(conn: psycopg.Connection, question: str) -> Resolution:
    """把问句解析成 G.4 的四种状态之一。"""
    mentions = find_mentions(conn, question)
    if not mentions:
        return Resolution(state=State.UNKNOWN)

    # ── 定作用域：作品名优先，其次角色名投票 ──────────────────────
    #
    # 🚨 **作品名和角色名不能平票**，这是 G.4 与第 15 节原则 4 的直接要求：
    #    「角色消歧必须锚定在已确认的 subject 范围内」（alias.parent_subject_id
    #    那一列的注释写的就是这句）。作品名定的是**作用域**，
    #    角色名是作用域**内**的实体，两者不是同一层的东西。
    #
    # ⚠️ 曾经把这条删掉、改成纯投票，实测立刻回归：
    #      「冰之城墙这个漫画的主人公是谁？」
    #        冰之城墙(subject) 1 票  vs  主人公(character) 撞 9 部各 1 票
    #        → 并列 → 判成 ambiguous，而用户明明已经点名了作品
    #    根因是「主人公」是个**泛称**却被收成了角色别名 ——
    #    CLAUDE.md 早就记过「主角重名 6.4%（アリス×9、主人公×9）」。
    #    ⇒ 有作品名时它就该被作用域直接消解掉，根本轮不到投票。
    subj_roots = {m.series_root for m in mentions if m.entity_type == "subject"}
    if len(subj_roots) > 1:
        # 点到了多部不同作品 —— 这是真歧义（也可能是在做对比，本层不猜）
        return Resolution(state=State.AMBIGUOUS, candidates=mentions,
                          mentions=mentions)
    if len(subj_roots) == 1:
        root = subj_roots.pop()
    else:
        # 没点作品名 → 按「被多少个**不同**角色名覆盖」投票，最高票唯一者胜出。
        #
        # ⚠️ 这条同样是实测逼出来的：「蕾姆和拉姆是什么关系」里蕾姆只在 Re:0、
        #    拉姆虽然撞 4 部但其中一部正是 Re:0，**交集唯一**。
        #    多个角色互相锚定是角色问答里最常见的形态，漏掉它反问会频繁到不可用。
        #
        # ⚠️ **并列时一律反问，不拿热度当决胜局。** G.4：猜错的代价不对称 ——
        #    反问多花一次点击，猜错是自信地讲了另一部作品的剧情，
        #    而用户很可能看不出来。实测「拉姆是谁」（撞 4 部）、
        #    「三笠对艾伦」（艾伦撞 6 部）都会走到这里。
        # ⚠️ 泛称先摘出去（见 GENERIC_NAMES）。全是泛称时退回用它们 ——
        #    「这部动画的主人公是谁」不该因此变成 UNKNOWN。
        voting = [m for m in mentions
                  if m.text not in GENERIC_NAMES] or mentions
        votes: dict[int, set[str]] = {}
        for m in voting:
            votes.setdefault(m.series_root, set()).add(m.text)
        ranked_roots = sorted(votes.items(), key=lambda kv: -len(kv[1]))
        if len(ranked_roots) > 1 and len(ranked_roots[0][1]) == len(ranked_roots[1][1]):
            return Resolution(state=State.AMBIGUOUS, candidates=mentions,
                              mentions=mentions)
        root = ranked_roots[0][0]

    return _scoped(conn, root, mentions)


def _scoped(conn: psycopg.Connection, root: int,
            mentions: list[Mention], *, title: str | None = None) -> Resolution:
    """作用域已经定下来之后的收尾：取标题、收角色 id、判有没有语料。

    ⚠️ 抽成函数是因为**有三个入口会走到这里**：正常 resolve、结构化续问
       （客户端回传 series_root）、以及上一轮作用域的继承。
       复制三份的话，「状态③ 必须短路」这条会在其中某一份里被漏掉，
       而漏掉的症状是 LLM 用训练记忆流畅编一个答案出来（G.4 状态③）。
    """
    mentions = [m for m in mentions if m.series_root == root]
    if title is None:
        title = next((m.title for m in mentions), None)
    char_ids = sorted({m.character_id for m in mentions
                       if m.character_id is not None})

    # 状态③：认出来了但作用域内没有任何语料。**必须在这里短路。**
    # G.4：检索为空还把问题丢给 LLM，它会用训练记忆流畅答出来 ——
    # 绕过整条 RAG 链路：没有出处、没有剧透门控、可能是幻觉。
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM plot_chunk_scope WHERE series_root = %s",
                    (root,))
        (n,) = cur.fetchone()
        if title is None:                  # 继承/回传来的 root 没有 mention 带标题
            cur.execute("SELECT coalesce(name_cn, name) FROM anime_profile "
                        "WHERE subject_id = %s", (root,))
            row = cur.fetchone()
            title = row[0] if row else None
    if n == 0:
        return Resolution(state=State.NO_CORPUS, series_root=root, title=title,
                          character_ids=char_ids, mentions=mentions)

    return Resolution(state=State.OK, series_root=root, title=title,
                      character_ids=char_ids, mentions=mentions)


def resolve_in_scope(conn: psycopg.Connection, question: str,
                     root: int) -> Resolution:
    """把作用域**钉死**在 root 上再解析（结构化续问 / 继承上一轮）。

    用于两种场景：
      ① 上一轮反问「你是指哪一部？」，客户端把用户选中的 series_root 传回来
      ② 追问句自己认不出实体（「那结局呢」），继承上一轮的作用域

    ⚠️ **仍然要跑 find_mentions**，因为角色 id 要在这个作用域内重新认一遍 ——
       「蕾姆」在别的作品里也有，钉死作用域正是为了让它只认对的那个。
    ⚠️ **不会因为 root 与问句无关就报错**：调用方（客户端）说了算，
       这一层不猜。传错 root 的后果是答非所问，但那是显式的、可回退的，
       比静默猜错好（G.4 那条「代价不对称」）。
    """
    mentions = [m for m in find_mentions(conn, question) if m.series_root == root]
    return _scoped(conn, root, mentions)


# ============================================================
# ② 召回
# ============================================================

_SELECT_COLS = """
    c.chunk_id, c.section, c.text, c.kind, c.source,
    c.character_id, c.spoiler_level
"""


def _row_to_chunk(row, *, pinned: bool = False) -> Chunk:
    return Chunk(chunk_id=row[0], section=row[1], text=row[2], kind=row[3],
                 source=row[4], character_id=row[5], spoiler_level=row[6],
                 pinned=pinned)


def pinned_chunks(conn: psycopg.Connection, character_ids: list[int],
                  *, spoiler: bool = False,
                  limit: int = MAX_PINNED) -> list[Chunk]:
    """① 的产物：点了名的角色，按 character_id 直取本人的 chunk。

    这一步**不经过向量**，所以不受「角色 chunk 正文里没有自己的名字」影响。

    ⚠️ limit 不是保险起见，见 MAX_PINNED 的注释。
    """
    if not character_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT {_SELECT_COLS}
              FROM plot_chunk c
             WHERE c.character_id = ANY(%s)
               AND (%s OR c.spoiler_level = 0)
             ORDER BY c.character_id, c.chunk_no
             LIMIT %s
        """, (character_ids, spoiler, limit))
        return [_row_to_chunk(r, pinned=True) for r in cur.fetchall()]


def recall(conn: psycopg.Connection, series_root: int, qvec: np.ndarray,
           *, spoiler: bool = False,
           quota_char: int = QUOTA_CHAR,
           quota_series: int = QUOTA_SERIES,
           quota_songs: int = QUOTA_SONGS) -> list[Chunk]:
    """② 作用域内向量召回，角色层 / 系列层 / 歌曲层各占配额。

    ⚠️ **作用域走 plot_chunk_scope，它的主键是 (series_root, chunk_id)** ——
       series_root 在前，所以这句吃 PK 索引，实测 0.1 ms，
       且不受语料从 2 万涨到 9 万的影响。这正是路径②（限定作品）
       不需要 HNSW 而路径③（跨作品全表）要 579 ms 的原因。

    ⚠️ 算子是 <#>（内积）不是 <=>（余弦距离）：向量已 L2 归一化
       （embed.py 实测范数 1.000000），内积等价余弦且更快，
       与 sql/007 的验收查询、src/embed.py 的注释保持一致。
       ⚠️ pgvector 的 <#> 返回的是**负内积**，所以 ASC 排序就是相似度降序。
    """
    # ⚠️ 格式化成字面量时用 .9g 不用 .7g：float32 无损往返需要 9 位有效数字，
    #    .7g 每元素会引入 1.49e-08 的偏差（B 节 parity 那次实测）。
    vec = "[" + ",".join(f"{x:.9g}" for x in qvec.astype(np.float32)) + "]"
    with conn.cursor() as cur:
        cur.execute(f"""
            WITH scoped AS (
                SELECT {_SELECT_COLS},
                       CASE WHEN c.character_id IS NOT NULL THEN 'char'
                            WHEN c.kind = 'songs'           THEN 'songs'
                            ELSE 'series' END AS layer,
                       row_number() OVER (
                           PARTITION BY
                               CASE WHEN c.character_id IS NOT NULL THEN 'char'
                                    WHEN c.kind = 'songs'           THEN 'songs'
                                    ELSE 'series' END
                           ORDER BY c.vec <#> %(q)s::halfvec
                       ) AS rn
                  FROM plot_chunk_scope s
                  JOIN plot_chunk c ON c.chunk_id = s.chunk_id
                 WHERE s.series_root = %(root)s
                   AND c.vec IS NOT NULL
                   AND (%(spoiler)s OR c.spoiler_level = 0)
            )
            SELECT chunk_id, section, text, kind, source,
                   character_id, spoiler_level
              FROM scoped
             WHERE rn <= CASE layer WHEN 'char'  THEN %(qc)s
                                    WHEN 'songs' THEN %(qsong)s
                                    ELSE %(qs)s END
             ORDER BY rn
        """, {"q": vec, "root": series_root, "spoiler": spoiler,
              "qs": quota_series, "qc": quota_char, "qsong": quota_songs})
        return [_row_to_chunk(r) for r in cur.fetchall()]


# ============================================================
# ③ rerank
# ============================================================

def _apply_pin_reserve(order: list[Chunk], final: int,
                       reserve: int = PIN_RESERVE,
                       min_score: float = MIN_SCORE,
                       min_keep: int = MIN_KEEP,
                       seat: int | None = None) -> list[Chunk]:
    """挑出最终交给 LLM 的 chunk：① 保底席位 + 相关度地板。

    **保底只保「在不在」，不保「排第几」** —— 让位之后仍按 rerank 分降序展示，
    所以被点名但确实不相关的 chunk 会老实地排在后面，不会伪装成最佳答案。

    ⚠️ **pinned 不受相关度地板约束。** 冈部那题他本人的 chunk 只有 0.0089，
       按地板会被砍掉 —— 而 PIN_RESERVE 存在的全部理由就是保住它
       （G.5g：alias 是「确定性的解法」，能被分数挤掉就不是确定性的）。
       用户点了名，这个信号比 reranker 的字面判断更强。
    """
    pins = [c for c in order if c.pinned][:reserve]
    pin_ids = {c.chunk_id for c in pins}

    # songs 保底席位：独立于 PIN_RESERVE，且**占座**而非豁免地板（见 SONGS_SEAT）。
    seats = [c for c in order
             if c.chunk_id == seat and c.chunk_id not in pin_ids][:SONGS_SEAT]
    held = pin_ids | {c.chunk_id for c in seats}
    others = [c for c in order if c.chunk_id not in held]

    room = max(0, final - len(pins) - len(seats))
    kept = [c for c in others if (c.score or 0.0) >= min_score][:room]
    if len(kept) < min_keep:
        kept = others[:min(min_keep, room)]

    out = pins + seats + kept
    out.sort(key=lambda c: -(c.score if c.score is not None else 0.0))
    return out[:final]


def songs_seat(question: str, pool: Sequence[Chunk]) -> int | None:
    """OP/ED 类问句 → 该给保底席位的那条 songs chunk 的 id；否则 None。

    取**层内第 1**（= 向量召回里最像的那条），不是 rerank 分最高的那条 ——
    rerank 恰恰是在这类查询上判不准的那一环，用它选席位等于绕回原点。
    ⚠️ `recall()` 的返回在层内保持 rn 序，所以"第一条 songs"就是层内第 1。
    """
    if not SONGS_QUERY.search(question):
        return None
    for c in pool:
        if c.kind == "songs" and c.character_id is None:
            return c.chunk_id
    return None


def retrieve(conn: psycopg.Connection, question: str, *,
             spoiler: bool = False, final: int = FINAL,
             res: Resolution | None = None) -> tuple[Resolution, list[Chunk], dict]:
    """①②③ 合起来：问句 → 最终 chunk 列表。**不含 ④，不调 LLM。**"""
    meta: dict = {}
    res = res or resolve(conn, question)
    meta["state"] = res.state.value
    if res.state is not State.OK:
        return res, [], meta

    pinned = pinned_chunks(conn, res.character_ids, spoiler=spoiler)
    qvec = embed.embed_query(question,
                             retries=REQUEST_EMBED_RETRIES,
                             timeout=REQUEST_EMBED_TIMEOUT)
    pool = recall(conn, res.series_root, qvec, spoiler=spoiler)

    # ① 与 ②③ 互补：直取的**并进**候选池而不是替换它。
    # 「三笠对艾伦是什么感情」这类描述性查询靠向量，「蕾姆是谁」靠直取，
    # 一条问句里两者可能同时需要。
    seen = {c.chunk_id for c in pinned}
    merged = pinned + [c for c in pool if c.chunk_id not in seen]
    meta.update(pinned=len(pinned), recalled=len(pool), pool=len(merged))

    if not merged:
        # resolve 确认过作用域里有 chunk，走到这里说明剧透门控把它们全滤掉了
        return res, [], meta

    try:
        # ⚠️ top_n 取全量而不是 final：cross-encoder 无论如何都要给每条打分，
        #    top_n 只截断响应 —— 要全量是**零额外成本**，而 PIN_RESERVE
        #    需要知道被点名角色的分数才能正确让位。
        ranked = rr.rerank(question, [c.text for c in merged], top_n=len(merged))
        order = []
        for idx, score in ranked:
            chunk = merged[idx]
            chunk.score = score
            order.append(chunk)
        out = _apply_pin_reserve(order, final,
                                 seat=songs_seat(question, pool))
        meta["reranked"] = True
        meta.update(rr.descriptor())
    except rr.RerankError as exc:
        # ⚠️ rerank 挂了**不让整个请求失败** —— 退回向量序仍然可用，
        #    只是质量下降（G.6：纯向量前 8 是 0/5，会明显变差但不是噪声）。
        #    这与 embedding 挂掉性质不同：那个的降级方向是纯 BM25（A.8）。
        out = merged[:final]
        meta["reranked"] = False
        meta["rerank_error"] = str(exc)

    return res, out, meta


# ============================================================
# ④ 生成
# ============================================================

def _trim(hist: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """把历史里每条消息截到 MAX_HISTORY_CHARS。

    🚨 **不截会把当前这轮的资料挤没。** I.2 ② 实测过：上下文里噪声一多，
       LLM 就从「答对」退化成「资料中没有提到」（同一题 8 条→拒答、3 条→答对）。
       历史是同一种噪声，而且**不受 MIN_SCORE 那道地板约束** ——
       地板管的是 chunk，管不到历史。⇒ 这里是唯一能拦住它的地方。
    ⚠️ 截断只影响喂给模型的副本，不改调用方持有的历史。
    """
    out = []
    for q, a in hist:
        out.append((q[:MAX_HISTORY_CHARS], (a or "")[:MAX_HISTORY_CHARS]))
    return out


def _last_scope(conn: psycopg.Connection,
                hist: Sequence[tuple[str, str]]) -> Resolution | None:
    """从最近的历史里倒着找一个能定出作用域的问句，返回它的整个 Resolution。

    ⚠️ **必须倒着走并且允许跳过**，不能只看上一轮：多轮追问会连成一串
       没有实体的句子（「三笠是谁」→「那结局呢」→「她和艾伦呢」），
       只重解析上一轮会拿到 UNKNOWN，**链条从第三轮就断了**。
    ⚠️ **返回整个 Resolution 而不只是 root**，因为 character_ids 也要带过来 ——
       理由见 ask() 里合并 pin 的那段（代词让向量召回失准，直取不受影响）。
    ⚠️ 代价是最多 MAX_HISTORY_TURNS 次 find_mentions（各一条走索引的查询），
       只在当前问句自己定不出作用域时才会走到这里。
    """
    for q, _ in reversed(list(hist)):
        r = resolve(conn, q)
        if r.series_root is not None:
            return r
    return None


def _candidate_labels(conn: psycopg.Connection,
                      candidates: list[Mention]) -> list[str]:
    """反问时展示的选项。**同名的要用年份区分开。**

    🚨 实测 bug：原先写的是 `{f"《{m.title}》" for m in candidates}` ——
       **按标题去重**，于是两个同名但不同 series_root 的作品被折成一个选项：
         「《多罗罗》的片头曲是什么？」→「你是指哪一部？《多罗罗》」
       1969 版(done=911) 与 2019 版(done=9450) 是两个系列根，
       用户看到唯一一个选项，**根本无从选择**，反问就此变成死路。
    ⚠️ 全库有 **81 个**这样的同名标题（忍者神龟×3 铁臂阿童木×3 狮子王×3
       Kanon×2…），正是 E.3 记过的「重制/不同改编同名」那批 —— 不是边角情况。
    ⇒ 按 series_root 去重（而不是按标题），并在标题相撞时补年份。
    """
    by_root = {m.series_root: m.title for m in candidates}
    if not by_root:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT subject_id, air_year, fav_done FROM anime_profile "
                    "WHERE subject_id = ANY(%s)", (list(by_root),))
        meta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    dup = {t for t in by_root.values()
           if list(by_root.values()).count(t) > 1}
    # 热度高的排前面 —— 用户更可能问的是那一部（但**不拿它当决胜局**自动选中）
    roots = sorted(by_root, key=lambda r: -(meta.get(r, (None, 0))[1] or 0))
    out = []
    for r in roots:
        year = meta.get(r, (None, None))[0]
        label = f"《{by_root[r]}》"
        if by_root[r] in dup and year:
            label += f"（{year}）"
        out.append(label)
    return out


def ask(conn: psycopg.Connection, question: str, *,
        spoiler: bool = False, final: int = FINAL,
        allow_fallback: bool = True,
        scope: int | None = None,
        history: Sequence[tuple[str, str]] | None = None) -> Answer:
    """完整四步。**chunks 为空时短路，绝不调 LLM**（G.4 状态③）。

    多轮对话（2026-08-19 加）：
      `scope`   客户端回传的 series_root —— 上一轮反问的答案。**零模型、确定性。**
      `history` 最近几轮 (问, 答)，交给 LLM 消解「她」「那结局呢」这类指代。

    ⚠️ **服务端不存任何会话状态**，历史由调用方传入 —— 与第 2 节那条
       「评分随请求传入」的架构铁律同源：游客的 localStorage 和将来注册用户的
       会话表都走同一个入口，这一层不区分。
    ⚠️ **两者的优先级不同**：`scope` 是显式指令，直接钉死作用域；
       `history` 只在**当前问句自己认不出实体时**才用来继承作用域 ——
       否则「聊完进击的巨人接着问芙莉莲」会被锁死在上一部作品里。
    """
    hist = list(history or ())[-MAX_HISTORY_TURNS:]

    res = None
    if scope is not None:
        # ① 结构化续问：用户点了候选，作用域已经确定
        res = resolve_in_scope(conn, question, scope)
    else:
        res = resolve(conn, question)
        if hist and res.state in (State.UNKNOWN, State.AMBIGUOUS):
            prev_res = _last_scope(conn, hist)
            prev = prev_res.series_root if prev_res else None
            if prev is not None:
                if res.state is State.UNKNOWN:
                    # ② 追问句自己没有实体（「那结局呢」）→ 继承上一轮的作用域。
                    res = resolve_in_scope(conn, question, prev)
                elif any(m.series_root == prev for m in res.candidates):
                    # ③ 追问句有实体但撞了多部，而**上一轮的作用域正是候选之一**
                    #    → 用它消歧，不要再问一遍用户刚说过的事。
                    #
                    # 🚨 **这条是端到端实跑才发现的，单元测试和设计都没料到。**
                    #    我原以为追问句会落在 UNKNOWN，实测「她和艾伦是什么关系？」
                    #    落的是 **AMBIGUOUS** —— 因为「艾伦」本身撞多部作品。
                    #    追问句最常见的形态恰恰是「代词 + 一个不唯一的实体」，
                    #    只处理 UNKNOWN 等于这条路径整个没接上。
                    res = resolve_in_scope(conn, question, prev)
                elif not any(m.entity_type == "subject" for m in res.candidates):
                    # ④ 歧义**全部来自角色名、问句一个作品名都没点** → 沿用上一轮。
                    #
                    # 🚨 实测：聊完进击的巨人再问「她和艾伦是什么关系？」，
                    #    候选是**另外 8 部**作品，进击的巨人根本不在其中 ——
                    #    因为它的艾伦存的是「艾伦·耶格尔」，而裸名「艾伦」
                    #    在 alias 里属于别人（55770 名下只有「艾伦·克鲁格」）。
                    #    ⇒ 这正是 I.1 ②「alias 只有官方书写形态、没有简称」那条缺口
                    #      在多轮场景里的形态：**裸名全局有歧义，在上下文里没有。**
                    #
                    # ⚠️ **必须限定「没点作品名」**：若问句自己点了作品
                    #    （「多罗罗的片头曲」→ 候选含 subject），那歧义是关于
                    #    「哪一部」的，硬套上一轮的作用域会答非所问。
                    res = resolve_in_scope(conn, question, prev)
            # ⚠️ 其余情况保持反问 —— 换话题了就别硬套旧作用域。

            # 继承成功时，把上一轮认出的角色也带过来做 ① 直取。
            #
            # 🚨 **只继承作用域是不够的，这是实测出来的。** 「三笠是谁」之后问
            #    「她和艾伦是什么关系？」，作用域正确继承到进击的巨人、召回了
            #    6 条 chunk，**LLM 仍然答「资料中没有提到」** —— 因为送去做向量
            #    召回的查询串里还留着代词「她」，召回的不是三笠那几条。
            #    而上一轮已经确定性地知道「她」= 三笠(character_id)，
            #    直取不经过向量，**不受代词影响**。
            # ⚠️ 只带**同一作用域**的角色（prev_res 本来就是那个作用域解析出来的），
            #    不会把别的作品的角色 pin 进来。
            if (prev_res is not None and res.state is State.OK
                    and res.series_root == prev):
                merged_ids = sorted(set(res.character_ids)
                                    | set(prev_res.character_ids))
                res.character_ids = merged_ids[:MAX_PINNED]

    res, chunks, meta = retrieve(conn, question, spoiler=spoiler,
                                 final=final, res=res)
    if hist:
        meta["history_turns"] = len(hist)

    if res.state is State.AMBIGUOUS:
        names = _candidate_labels(conn, res.candidates)
        return Answer(res.state, f"你是指哪一部？{' / '.join(names[:5])}",
                      [], res, meta)
    if res.state is State.UNKNOWN:
        return Answer(res.state, "没认出你问的是哪部作品或哪个角色。", [], res, meta)
    # ── 结构化关联查询（src/related.py）────────────────────────────
    # 🚨 「这个作者/导演还做过什么」**不该走 RAG**：② 召回写死了
    #    WHERE series_root = X，按设计就看不到别的作品的 chunk。
    #    实测「冰之城墙的作者还画过其他漫画吗」→「资料中没有提到」，
    #    而 staff 列里一条 SQL 就能查出《相反的你和我》。
    # ⚠️ 它拼成一条普通的 (section, text) 走同一个通道进 prompt ——
    #    不给 LLM 开第二个信息入口，否则「资料」在 prompt 里就有两种含义。
    rel = (related.lookup(conn, question, res.series_root)
           if res.series_root else [])
    extra = related.as_context(rel)
    if rel:
        meta["related"] = [
            {"series_root": r.series_root, "title": r.name_cn or r.name,
             "year": r.air_year, "via_role": r.via_role, "via_name": r.via_name}
            for r in rel
        ]

    if res.state is State.NO_CORPUS or not chunks:
        # ⚠️ 就算没有剧情语料，关联查询的结果仍然可能回答得了问题 ——
        #    那批事实来自结构化字段，与 plot_chunk 有没有覆盖无关。
        if not extra:
            return Answer(State.NO_CORPUS,
                          f"《{res.title}》在资料库里没有可用的剧情语料。",
                          [], res, meta)
        text, served_by = llm.answer(question, [extra],
                                     allow_fallback=allow_fallback,
                                     history=_trim(hist))
        meta.update(llm.descriptor(served_by))
        return Answer(State.OK, text, [], res, meta)

    pairs = [c.as_llm_pair() for c in chunks]
    if extra:
        pairs.append(extra)
    text, served_by = llm.answer(question, pairs, allow_fallback=allow_fallback,
                                 history=_trim(hist))
    meta.update(llm.descriptor(served_by))
    return Answer(State.OK, text, chunks, res, meta)

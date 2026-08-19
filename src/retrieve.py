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

from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import psycopg

from src import embed, llm, textproc
from src import rerank as rr

# ── 参数（G.6 实测定案，改之前先重跑那轮召回宽度扫描）─────────────
# 目标 chunk 在纯向量里的真实排名：top-4 / 13 / 9 / 21 / 38
#   k=20 两道够不着 · k=30 一道够不着 · k=50 全中 · k=80 无额外收益
QUOTA_CHAR = 40      # 角色层配额
QUOTA_SERIES = 10    # 系列层保底（prose）
QUOTA_SONGS = 3      # 歌曲层保底 —— 见下
FINAL = 8            # 交给 LLM 的条数

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
PIN_RESERVE = 4

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

    # 按「被多少个**不同**实体名覆盖」给候选作品投票，最高票唯一者胜出。
    #
    # ⚠️ 这条规则是实测逼出来的。第一版只在「作品名被点到」时收敛，于是
    #    「蕾姆和拉姆是什么关系」被判成歧义 —— 而蕾姆只在 Re:0、拉姆虽然
    #    撞 4 部但其中一部正是 Re:0，**交集唯一**。多个角色互相锚定是
    #    角色问答里最常见的形态，漏掉它会让反问频繁到不可用。
    #    💡 「Re:0 的拉姆」那种作品名锚定被这条规则自然涵盖（作品名也是一票），
    #       所以不需要为 subject 单独写分支。
    #
    # ⚠️ **但并列时一律反问，不拿热度当决胜局。** G.4：猜错的代价不对称 ——
    #    反问多花一次点击，猜错是自信地讲了另一部作品的剧情，而用户很可能
    #    看不出来。实测「拉姆是谁」（撞 4 部）、「三笠对艾伦」（艾伦撞 6 部）
    #    都走这条路径，角色名跨作品重名率 4.10%，它是真的会被走到。
    votes: dict[int, set[str]] = {}
    for m in mentions:
        votes.setdefault(m.series_root, set()).add(m.text)
    ranked_roots = sorted(votes.items(), key=lambda kv: -len(kv[1]))
    if len(ranked_roots) > 1 and len(ranked_roots[0][1]) == len(ranked_roots[1][1]):
        return Resolution(state=State.AMBIGUOUS, candidates=mentions,
                          mentions=mentions)

    root = ranked_roots[0][0]
    mentions = [m for m in mentions if m.series_root == root]
    title = next(m.title for m in mentions if m.series_root == root)
    char_ids = sorted({m.character_id for m in mentions
                       if m.character_id is not None})

    # 状态③：认出来了但作用域内没有任何语料。**必须在这里短路。**
    # G.4：检索为空还把问题丢给 LLM，它会用训练记忆流畅答出来 ——
    # 绕过整条 RAG 链路：没有出处、没有剧透门控、可能是幻觉。
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM plot_chunk_scope WHERE series_root = %s",
                    (root,))
        (n,) = cur.fetchone()
    if n == 0:
        return Resolution(state=State.NO_CORPUS, series_root=root, title=title,
                          character_ids=char_ids, mentions=mentions)

    return Resolution(state=State.OK, series_root=root, title=title,
                      character_ids=char_ids, mentions=mentions)


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
                       reserve: int = PIN_RESERVE) -> list[Chunk]:
    """保证 ① 点名角色的 chunk 出现在最终结果里（见 PIN_RESERVE 的注释）。

    **只保「在不在」，不保「排第几」** —— 让位之后仍按 rerank 分降序展示，
    所以被点名但确实不相关的 chunk 会老实地排在后面，不会伪装成最佳答案。
    """
    pins = [c for c in order if c.pinned][:reserve]
    pin_ids = {c.chunk_id for c in pins}
    others = [c for c in order if c.chunk_id not in pin_ids]
    out = pins + others[:max(0, final - len(pins))]
    out.sort(key=lambda c: -(c.score if c.score is not None else 0.0))
    return out[:final]


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
        out = _apply_pin_reserve(order, final)
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

def ask(conn: psycopg.Connection, question: str, *,
        spoiler: bool = False, final: int = FINAL,
        allow_fallback: bool = True) -> Answer:
    """完整四步。**chunks 为空时短路，绝不调 LLM**（G.4 状态③）。"""
    res, chunks, meta = retrieve(conn, question, spoiler=spoiler, final=final)

    if res.state is State.AMBIGUOUS:
        names = sorted({f"《{m.title}》" for m in res.candidates})
        return Answer(res.state, f"你是指哪一部？{' / '.join(names[:5])}",
                      [], res, meta)
    if res.state is State.UNKNOWN:
        return Answer(res.state, "没认出你问的是哪部作品或哪个角色。", [], res, meta)
    if res.state is State.NO_CORPUS or not chunks:
        return Answer(State.NO_CORPUS,
                      f"《{res.title}》在资料库里没有可用的剧情语料。", [], res, meta)

    text, served_by = llm.answer(question, [c.as_llm_pair() for c in chunks],
                                 allow_fallback=allow_fallback)
    meta.update(llm.descriptor(served_by))
    return Answer(State.OK, text, chunks, res, meta)

"""声优与配役查询 —— 「XXX 配过哪些角色」「XX 是谁配的」的正解。

🚨 **这类问题不该走 RAG**，理由与 related.py 完全相同：答案是**结构化事实**，
   而 chunk 检索只能靠语义碰运气。实测钉宫理惠在库里有 478 条配役，
   靠向量召回最多凑出几条散落在各作品页里的提及。
   走 voice_role 是一条 SQL：精确、免费、零幻觉。

⚠️ **但它和 related.py 有一处关键差异：这里必须从问句里抠人名。**
   related.py 开篇警告过这个坑（staff 存日文原形「阿賀沢紅茶」，正文用简体
   「阿贺泽红茶」，繁简重合度极低），它靠 id → staff → 其他作品绕开了。
   声优绕不开 —— 用户就是拿名字来问的。
   ⇒ 解法是**把声优名灌进 alias 表**（sql/009 加了 entity_type='person'），
     用 norm_name 精确匹配，而不是去做繁简/变体的模糊匹配。
     实测「花泽香菜」→ 花澤香菜、「钉宫理惠」→ 釘宮理恵 都能对上。

📌 **对现有解析零影响**：retrieve.find_mentions 的 SQL 是
     JOIN anime_profile ON p.subject_id = coalesce(a.parent_subject_id, a.subject_id)
   而声优行这两列都是 NULL，INNER JOIN 天然把它们排除在外。
   所以这 28,643 行别名不会污染作品/角色的解析结果。

数据规模（2026-08-21 实测）：
    person 8,215 人 · voice_role 145,306 条 · 覆盖 8,529 部作品
    角色名靠 alias 反查：45.9% 有中文名、23.2% 只有日文/罗马音、
    **30.9% 完全查不到** —— 那些是没写简介的边缘角色（Kevin 判断可接受，不补）。
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from src import textproc

# ⚠️ 复用 retrieve 的子串枚举而**不复制一份** —— 两份实现迟早漂移，
#    而这个函数的边界条件（长度上下限）是实测标定过的。
from src.retrieve import _substrings

# 「这个人配过什么」——问的是声优的作品表。
PERSON_TRIGGERS = (
    "配过", "配了", "演过", "出演", "配音作品", "参演",
    "代表作", "有哪些角色", "哪些角色", "什么角色",
)
# 「这个角色是谁配的」——问的是某个角色的声优。
CHARACTER_TRIGGERS = (
    "谁配", "声优是", "声优谁", "cv是", "cv谁", "谁的声优",
    "配音是", "哪位声优", "什么声优",
)
# 只出现这些词不足以判定，但和上面任一组合就更确定。
SOFT_TRIGGERS = ("声优", "配音", "cv", "seiyu")

MAX_ROLES = 20          # 一次最多列多少个配役
MIN_NAME = 2            # 少于 2 字的"人名"一律不认，避免噪声

# 送进 LLM 的资料只取前这么多条 —— **返回给前端的 items 不受它限制**。
# ⚠️ 两个数分开的理由：前端要的是完整明细（用户会往下翻），而 LLM 只需要
#    够它挑 5~8 个来讲；资料越长首 token 延迟越高，且模型越容易改去复述整张表。
#    实测 20 条 → 输出 350 字 / 34~41 秒，是这次要压下去的主要成本。
CONTEXT_ROLES = 12

# ── 列表排序口径（2026-08-25 加）──────────────────────────────────
# 「花泽香菜**最近**配过什么角色」这类问句要的是按年份排，而不是按热度。
ORDER_POPULAR = "popular"   # 热度降序 —— 默认，回答「配过哪些角色」
ORDER_RECENT = "recent"     # 播出年份降序 —— 回答「最近配过什么」

# ⚠️ **时间意图用规则判，不调模型** —— 与 router.parse_cour 同一条纪律：
#    这是有限的一组模式，交给 LLM 反而会得到不稳定的结果，而排序错了
#    整个列表就答非所问。
# ⚠️ 有意**不收**「新番」「现在」这类：「她在新番里配了谁」问的是具体作品，
#    「现在」在中文里更多是语气词（「现在她配过哪些角色」= 到目前为止），
#    收进来会把普通的作品表查询错判成时间查询。
RECENT_TRIGGERS = (
    "最近", "近期", "最新", "近年", "近几年", "这几年", "这两年", "今年",
)

# 🚨 **排序热度要按役别打折，纯 `fav_done` 降序实测明显更差**（2026-08-25 实测）。
#    Kevin 报「按作品的热度排个序」，第一版就照字面做了纯热度降序，结果：
#      钉宫理惠   「神乐」从第 3 掉到 **第 21**（直接掉出前 20），
#                 前 12 条里 9 条是配角/客串 —— 《游戏人生》特图、
#                 《咒术回战》西宫桃这类路人役顶掉了夏娜、露易丝
#      花泽香菜   《埃罗芒阿老师》的客串排第 6、《天气之子》里她**本人**
#                 客串（角色名就叫「花泽香菜」）排第 7，
#                 挤掉了小渊泽报濑、中野一花
#    根因很直白：热门作品的角色**总数**远多于冷门作品，所以纯热度排序等价于
#    "按作品热度列角色"，而用户问的是「这位声优配过哪些**角色**」。
#    ⇒ 用 `fav_done × 役别权重` 排，既保住了热度这个主轴，又不让客串役屠榜。
#
# ⚠️ 这几个权重是**排序偏好**，不是判据阈值 —— 与 B.4「rerank 绝对地板调不稳」
#    不是一类问题：调错了只是顺序略有不同，不会静默给出错误答案，而且
#    列表本身就能直接查验。标定方式：0.35 让 done=44,372 的配角（《游戏人生》
#    特图）折算成 ≈15.5k，正好落在夏娜(16.1k)之后、露易丝(15.6k)之前 ——
#    **知名配角进得来，但挤不掉主线代表役**，这就是想要的位置。
# ⚠️ `role_type` 仍然**只用于排序，不用于过滤**（sql/009 的原话）。
ROLE_WEIGHT = {1: 1.0, 2: 0.35, 3: 0.12}
# 未标注役别的按配角算：voice_role 里 role_type 可能为 NULL，
# 当主角处理会让一批未知记录压过真主角，当客串又会把它们埋掉。
ROLE_WEIGHT_DEFAULT = 0.35


def _weighted_heat(role: Role) -> float:
    """排序用的热度分：作品热度 × 役别权重。见 ROLE_WEIGHT 上面那段。"""
    return (role.fav_done or 0) * ROLE_WEIGHT.get(role.role_type,
                                                  ROLE_WEIGHT_DEFAULT)


@dataclass(frozen=True)
class Role:
    """一条配役记录（已按系列折叠）。"""
    character_id: int
    character_name: str | None
    series_root: int
    title: str
    air_year: int | None
    role_type: int | None       # 1=主角 2=配角 3=客串，可能为 None
    fav_done: int | None


@dataclass(frozen=True)
class Person:
    person_id: int
    name: str                   # dump 原名，多为日文
    name_cn: str | None
    n_roles: int


def wants(question: str) -> str | None:
    """问句想查什么。返回 'person' / 'character' / None。

    ⚠️ 顺序有意义：「谁配的」比「配过」更具体，先判它。
       实测「银魂里神乐是谁配的」两组词都命中，正确答案是 character。
    """
    q = question.lower()
    if any(k in q for k in CHARACTER_TRIGGERS):
        return "character"
    if any(k in q for k in PERSON_TRIGGERS) and any(k in q for k in SOFT_TRIGGERS):
        return "person"
    if any(k in q for k in ("配过", "配了", "配音作品")):
        return "person"
    return None


def wants_recent(question: str) -> bool:
    """问句要的是「最近配了什么」还是「配过哪些角色」。

    ⚠️ **这只切换排序，不做时间过滤。** 过滤会让「最近」变成一个需要标定的
       年份阈值（近 1 年？近 3 年？），而库里最晚的 air_date 只到 2026-09-11
       且候选集 `done>=50` 天然挡住刚开播的新番 —— 阈值卡紧就返回空列表，
       用户会以为这个声优近年没作品。排序则不会丢东西：最新的排最前，
       后面还有什么一目了然。
    """
    return any(k in question for k in RECENT_TRIGGERS)


def find_person(conn: psycopg.Connection, question: str) -> list[Person]:
    """扫出问句里能对上的声优，按配役数降序。

    ⚠️ 归一化必须走 textproc.norm_name()，与 find_mentions 同一条纪律 ——
       库里存的是 NFKC + casefold + 去标点的形态。

    ⚠️ **同名声优按配役数排序而不是反问。** 与 G.4「并列一律反问」不同，
       理由是代价结构不同：作品/角色猜错会讲出另一部作品的剧情而用户看不出来；
       而声优同名极罕见，且列表本身自带作品名，用户一眼能看出认错了人。
    """
    norm_q = textproc.norm_name(question)
    cands = [c for c in _substrings(norm_q) if len(c) >= MIN_NAME]
    if not cands:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.person_id, p.name, p.name_cn, count(v.*) AS n
              FROM alias a
              JOIN person p ON p.person_id = a.person_id
              LEFT JOIN voice_role v ON v.person_id = p.person_id
             WHERE a.entity_type = 'person' AND a.norm_name = ANY(%s)
             GROUP BY p.person_id, p.name, p.name_cn
             HAVING count(v.*) > 0
             ORDER BY n DESC
        """, (cands,))
        return [Person(*r) for r in cur.fetchall()]


# ⚠️ 取角色名的优先级：中文名 > 日文原名 > 其他别名。
#    实测不排会拿到罗马音（「Aisaka Taiga」而不是「逢坂大河」），
#    而日文原名对中文读者反而可读（多是汉字）—— 罗马音才是最差的那档。
_NAME_PICK = """
    SELECT name FROM alias
     WHERE character_id = v.character_id AND entity_type = 'character'
     ORDER BY CASE source WHEN 'char_name_cn' THEN 0
                          WHEN 'char_name'    THEN 1 ELSE 2 END
     LIMIT 1
"""


def roles_of(conn: psycopg.Connection, person_id: int,
             limit: int = MAX_ROLES,
             order: str = ORDER_POPULAR) -> list[Role]:
    """某声优的配役列表。`order` 决定按热度还是按播出年份排。

    ⚠️ **按 character_id 折叠，比 series_root 更狠一档。** 先只折 series_root
       时实测仍有重复：「逢坂大河《龙与虎》」和「逢坂大河《龙与虎OVA 便当的精髓》」
       各占一行 —— 因为 OVA 自成一个 root（第四部分挂着的「rt=12 衍生没折叠」）。
       而这个列表回答的是「配过哪些**角色**」，同一角色列一次就够，
       重复只会把别的角色挤出前 20。⇒ 每个 character_id 取热度最高的那部作代表。
       📌 反过来《血界战线》里的玛丽/威廉/绝望王是三个不同 character_id，
          照常各占一行 —— 折的是同一角色的重复出演，不是同一部作品的多个角色。

    🚨 **选代表作时 role_type 必须排在热度之前。** 只按热度会出这种错：
       「神乐」在《齐木楠雄的灾难 第二季》(done=27,828) 是客串、在《银魂》
       (done=18,539) 是主角 —— 按热度选中客串那条，接着又因为 role_type≠1
       被排到列表末尾，**结果钉宫理惠的代表作里整个看不到神乐**。
       同一个角色，要取她担纲主角的那部作品作代表。

    ⚠️ **role_type 只用于排序，不用于过滤**（sql/009 里写了理由）：
       「花泽香菜配过哪些角色」只答主角役就是答错。

    🚨 **这个函数里有两层排序，作用完全不同，别把它们混成一件事**：
       ① SQL 的 `DISTINCT ON` 内部 —— 决定同一个角色**取哪部作品作代表**。
          这一层 `role_type` 必须压过热度（就是上面神乐那条，别改）。
       ② 返回前的最终排序 —— 决定**列表里谁排前面**，由 `order` 参数控制。
       2026-08-25 之前 ② 是「先按是不是主角分两档，档内再按热度」；现在改成
       连续的加权热度（见 ROLE_WEIGHT）。**只改 ② 不动 ①。**
       📌 两者差别其实不大 —— 实测钉宫理惠/花泽香菜的前 12 条几乎一样，
          唯一变化是《游戏人生》特图这类**超热门作品里的知名配角**能进榜了。
          真正被这次实测否掉的是"纯热度"（见 ROLE_WEIGHT 上面那段）。

    ⚠️ 两种 order 的末位键都带 `character_id`，为的是**排序确定性** ——
       `fav_done` / `air_year` 大量并列（NULL 尤其多），不带次级键的话
       同一次查询两次调用可能给出不同顺序，与 recommend_sql 那条
       「ORDER BY 必须带次级键 subject_id」是同一条纪律。
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT DISTINCT ON (v.character_id)
                   v.character_id, ({_NAME_PICK}) AS cname,
                   ap.series_root, coalesce(ap.name_cn, ap.name) AS title,
                   ap.air_year, v.role_type, ap.fav_done
              FROM voice_role v
              JOIN anime_profile ap ON ap.subject_id = v.subject_id
             WHERE v.person_id = %s
             ORDER BY v.character_id,
                      (v.role_type = 1) DESC NULLS LAST,   -- 先要「她在这部是主角」
                      ap.fav_done DESC NULLS LAST
        """, (person_id,))
        rows = [Role(*r) for r in cur.fetchall()]
    if order == ORDER_RECENT:
        # 年份未知的排最后：`or 0` 让它们变成"最古老"，正是我们要的 ——
        # 问「最近」时把一条没有年份的记录摆在最前是纯粹的误导。
        # ⚠️ 同一年内仍按加权热度，否则同年的客串会盖过同年的主角役。
        rows.sort(key=lambda r: (-(r.air_year or 0), -_weighted_heat(r),
                                 r.character_id))
    else:
        rows.sort(key=lambda r: (-_weighted_heat(r), r.character_id))
    return rows[:limit]


def voices_of(conn: psycopg.Connection, character_ids: list[int]) -> list[tuple]:
    """某些角色分别是谁配的。返回 (character_id, character_name, person) 列表。

    ⚠️ 一个角色可能有多个声优（少年期/成年期、不同作品版本），全部返回，
       由调用方决定怎么呈现 —— 截断会让「幼年由 XX 配」这类事实消失。
    """
    if not character_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT v.character_id,
                   (SELECT name FROM alias
                     WHERE character_id = v.character_id AND entity_type = 'character'
                     ORDER BY CASE source WHEN 'char_name_cn' THEN 0
                                          WHEN 'char_name'    THEN 1 ELSE 2 END
                     LIMIT 1) AS cname,
                   p.person_id, p.name, p.name_cn,
                   coalesce(ap.name_cn, ap.name) AS title
              FROM voice_role v
              JOIN person p ON p.person_id = v.person_id
              JOIN anime_profile ap ON ap.subject_id = v.subject_id
             WHERE v.character_id = ANY(%s)
             ORDER BY v.character_id, ap.fav_done DESC NULLS LAST
        """, (character_ids,))
        return cur.fetchall()


def as_context(person: Person, roles: list[Role],
               order: str = ORDER_POPULAR) -> tuple[str, str] | None:
    """拼成一条可以塞进 llm.answer() 的 (section, text)。

    ⚠️ 走的是和 chunk 完全一样的通道，**不给 LLM 开第二个信息入口** ——
       与 related.as_context 同一条理由：否则「资料」这个概念在 prompt 里
       就有两种含义，而 ANSWER_SYSTEM 第 1 条正是靠它划边界的。

    ⚠️ 角色名缺失时**如实写「（角色名缺失）」而不是跳过这一行** ——
       跳过会让配役总数和列出的条数对不上，模型可能据此编一个。
    """
    if not roles:
        return None
    who = person.name_cn or person.name
    lines = []
    for r in roles:
        tag = {1: "主角", 2: "配角", 3: "客串"}.get(r.role_type, "")
        name = r.character_name or "（角色名缺失）"
        year = f"（{r.air_year}）" if r.air_year else ""
        lines.append(f"{name} —— 《{r.title}》{year}" + (f"　{tag}" if tag else ""))

    how = ("按播出年份从新到旧" if order == ORDER_RECENT
           else "按作品热度从高到低（主角役权重更高）")
    # ⚠️ **必须写明这是截断后的前 N 条，而不是全部。** 不写的话模型会把
    #    「列了 20 条」理解成「她一共就配过 20 个角色」，进而说出「作品不多」
    #    这类与 n_roles 直接矛盾的话 —— 与「角色名缺失要如实标注」同一条
    #    理由：**别让模型自己去补一个数**。
    head = (f"{who}（{person.name}）在库内共有 {person.n_roles} 条配役记录，"
            f"下面是其中 {len(roles)} 条，{how}排列"
            f"（同一个角色只列一次，取其担纲主角或最热门的那部作品作代表）：")
    return ("声优配役（来自数据库的结构化字段，非剧情语料）",
            head + "\n" + "\n".join(lines))


def lookup(conn: psycopg.Connection, question: str) -> tuple[Person, list[Role]] | None:
    """问句 → (声优, 配役列表)。认不出来返回 None，由调用方走别的分支。

    ⚠️ 排序由问句自己决定（`wants_recent`）—— 调用方不要再判一次，
       否则「最近」这个判据就有两个定义处，迟早漂移。
    """
    people = find_person(conn, question)
    if not people:
        return None
    person = people[0]
    order = ORDER_RECENT if wants_recent(question) else ORDER_POPULAR
    return person, roles_of(conn, person.person_id, order=order)

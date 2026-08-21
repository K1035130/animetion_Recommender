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
             limit: int = MAX_ROLES) -> list[Role]:
    """某声优的配役列表，主角优先 + 热度降序。

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
    rows.sort(key=lambda r: (r.role_type != 1, -(r.fav_done or 0)))
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


def as_context(person: Person, roles: list[Role]) -> tuple[str, str] | None:
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
    head = (f"{who}（{person.name}）在库内共有 {person.n_roles} 条配役记录，"
            f"以下按主角优先、热度降序列出前 {len(roles)} 条：")
    return ("声优配役（来自数据库的结构化字段，非剧情语料）",
            head + "\n" + "\n".join(lines))


def lookup(conn: psycopg.Connection, question: str) -> tuple[Person, list[Role]] | None:
    """问句 → (声优, 配役列表)。认不出来返回 None，由调用方走别的分支。"""
    people = find_person(conn, question)
    if not people:
        return None
    person = people[0]
    return person, roles_of(conn, person.person_id)

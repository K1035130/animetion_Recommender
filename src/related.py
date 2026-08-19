"""结构化关联查询 —— 「这个作者/导演还做过什么」这类问题的正解。

🚨 **这类问题不该走 RAG。** 触发它的实测案例：

    问「冰之城墙这个漫画的作者还画过其他漫画吗？」
      流程 C 返回「资料中没有提到」—— 而且**它是对的**：
      ② 召回写死了 WHERE series_root = X，按设计就看不到别的作品的 chunk，
      而「阿贺泽红茶」在全库只出现 2 次，另一次在《相反的你和我》名下。

    同一个问题走结构化字段是一条 SQL：
      SELECT ... WHERE staff @> '[{"name":"阿賀沢紅茶","role":"原作"}]'
      → 相反的你和我(2026) · 冰之城墙(2026) · 相反的你和我 第二季(2026)

   精确、免费、零幻觉，而且**比建 HNSW 划算得多** —— 路径③（跨作品向量检索）
   实测 579 ms 且要 230–280 MB 索引，却仍然只能靠语义碰运气命中这种事实。

⚠️ **人名一律从「已解析作品自己的 staff 列」里取，绝不从问句里抠。**
   staff 存的是日文原形（阿賀沢紅茶），而萌娘正文用简体（阿贺泽红茶），
   两者字符重合度极低 —— 第 13 节记过同一个坑（冨樫義博 vs 富坚义博
   重合率不到 20%），当时的结论是「繁简变体自动匹配不可靠」。
   走 id → staff → 其他作品 这条路，**根本不需要匹配名字**，坑自然消失。

覆盖率（实测）：
    导演 9,230 · 脚本 8,649 · 原作 8,166 · 人物设定 8,038 · 音乐 7,270
    studios 10,576   —— 全部按作品数计，库内共 11,453 部
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import psycopg

# 库里实际存在的 role 值（实测枚举，不要凭猜加）。
STAFF_ROLES = ("导演", "脚本", "原作", "人物设定", "音乐")

# 问句关键词 → 要查的 facet。
#
# ⚠️ **这是关键词触发，不是意图分类器。** 第 15 节原则 2 那条
#    「检索前门控 > 运行时 LLM 检测」同样适用：能用规则判的别交给模型 ——
#    规则确定、免费、可测；模型判会飘，而且要花一次调用才知道它不该被调用。
#    误触发的代价也很小：多跑一条走索引的 SQL，答案里多几行事实。
ROLE_TRIGGERS: dict[str, tuple[str, ...]] = {
    "原作": ("原作", "作者", "漫画家", "画过", "写过", "原著"),
    "导演": ("导演", "监督", "执导"),
    "脚本": ("脚本", "编剧", "剧本"),
    "音乐": ("音乐", "配乐", "作曲", "劇伴"),
    "人物设定": ("人设", "人物设定", "角色设计", "作画监督"),
}
STUDIO_TRIGGERS = ("制作公司", "制作方", "动画公司", "工作室", "哪家公司", "哪个公司")

# 「还有别的吗」这类问法 —— 单独出现时默认查 原作 + 导演 + studios。
GENERIC_TRIGGERS = ("其他作品", "还做过", "还画过", "还有什么作品", "别的作品", "同一个人")


@dataclass(frozen=True)
class Related:
    series_root: int
    subject_id: int
    name: str
    name_cn: str | None
    air_year: int | None
    fav_done: int
    # 因为哪个 facet 关联上的：("原作", "阿賀沢紅茶") 或 ("制作公司", "CloverWorks")
    via_role: str
    via_name: str


def wants(question: str) -> tuple[list[str], bool]:
    """问句想查哪些 staff role、要不要查制作公司。

    返回 (roles, want_studio)。两个都空 = 这不是一个关联查询，调用方跳过。
    """
    roles = [r for r, kws in ROLE_TRIGGERS.items() if any(k in question for k in kws)]
    want_studio = any(k in question for k in STUDIO_TRIGGERS)
    if not roles and not want_studio and any(k in question for k in GENERIC_TRIGGERS):
        # 泛问「还做过什么」→ 查最能定义一部作品身份的两个 role + 公司
        return ["原作", "导演"], True
    return roles, want_studio


def _fold(rows: list[tuple]) -> list[Related]:
    """按 series_root 折叠 —— 续作不算「另一部作品」。

    ⚠️ 与推荐链路的续作折叠同一个键（anime_profile.series_root），
       不要另起一套：实测「相反的你和我」与「相反的你和我 第二季」
       共用 root=525565，分开列出来只是噪声。
    每个系列保留代表作：优先系列根本身，否则取热度最高的那部。
    """
    best: dict[int, tuple] = {}
    for row in rows:
        root, sid, done = row[0], row[1], row[5]
        cur = best.get(root)
        if cur is None:
            best[root] = row
            continue
        # 系列根本身优先；否则热度高的赢
        cur_is_root, new_is_root = cur[1] == cur[0], sid == root
        if (new_is_root and not cur_is_root) or (
                new_is_root == cur_is_root and done > cur[5]):
            best[root] = row
    out = [Related(series_root=r[0], subject_id=r[1], name=r[2], name_cn=r[3],
                   air_year=r[4], fav_done=r[5], via_role=r[6], via_name=r[7])
           for r in best.values()]
    out.sort(key=lambda x: -x.fav_done)
    return out


def same_staff(conn: psycopg.Connection, series_root: int, roles: list[str],
               *, limit: int = 20) -> list[Related]:
    """与给定作品共享某个 staff 岗位的其他作品。

    ⚠️ 人名从本作品的 staff 列里读出来再反查，**不做任何文本匹配**（见模块注释）。
    """
    if not roles:
        return []
    with conn.cursor() as cur:
        # ① 本系列各部的 staff 里，指定 role 的人名
        cur.execute("""
            SELECT DISTINCT e->>'role', e->>'name'
              FROM anime_profile, jsonb_array_elements(staff) e
             WHERE series_root = %s AND e->>'role' = ANY(%s)
        """, (series_root, roles))
        people = cur.fetchall()
        if not people:
            return []

        rows: list[tuple] = []
        for role, name in people:
            cur.execute("""
                SELECT series_root, subject_id, name, name_cn, air_year, fav_done,
                       %s::text, %s::text
                  FROM anime_profile
                 WHERE staff @> %s::jsonb
                   AND series_root <> %s
                 ORDER BY fav_done DESC
                 LIMIT %s
            """, (role, name,
                  json.dumps([{"name": name, "role": role}], ensure_ascii=False),
                  series_root, limit))
            rows.extend(cur.fetchall())
    return _fold(rows)[:limit]


def same_studio(conn: psycopg.Connection, series_root: int,
                *, limit: int = 20) -> list[Related]:
    """同制作公司的其他作品。

    ⚠️ studios 是 text[] 不是 jsonb，用 && 判数组相交。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT unnest(studios) FROM anime_profile
             WHERE series_root = %s AND studios IS NOT NULL
        """, (series_root,))
        names = [r[0] for r in cur.fetchall()]
        if not names:
            return []
        rows: list[tuple] = []
        for name in names:
            cur.execute("""
                SELECT series_root, subject_id, name, name_cn, air_year, fav_done,
                       '制作公司'::text, %s::text
                  FROM anime_profile
                 WHERE studios && ARRAY[%s]::text[]
                   AND series_root <> %s
                 ORDER BY fav_done DESC
                 LIMIT %s
            """, (name, name, series_root, limit))
            rows.extend(cur.fetchall())
    return _fold(rows)[:limit]


def lookup(conn: psycopg.Connection, question: str, series_root: int,
           *, limit: int = 8) -> list[Related]:
    """问句触发了关联查询就跑，否则返回空列表。"""
    roles, want_studio = wants(question)
    if not roles and not want_studio:
        return []
    out = same_staff(conn, series_root, roles, limit=limit)
    if want_studio:
        seen = {r.series_root for r in out}
        out += [r for r in same_studio(conn, series_root, limit=limit)
                if r.series_root not in seen]
    return out[:limit]


def as_context(items: list[Related]) -> tuple[str, str] | None:
    """把结果拼成一条可以塞进 llm.answer() 的 (section, text)。

    ⚠️ 走的是和 chunk 完全一样的通道，**不给 LLM 开第二个信息入口** ——
       否则「资料」这个概念在 prompt 里就有两种含义，而 ANSWER_SYSTEM
       第 1 条（只能依据给出的资料）正是靠这个概念划边界的。
    """
    if not items:
        return None
    lines = [
        f"{r.name_cn or r.name}"
        + (f"（{r.air_year}）" if r.air_year else "")
        + f" —— 与本作共享 {r.via_role}：{r.via_name}"
        for r in items
    ]
    return ("关联作品（来自数据库的结构化字段，非剧情语料）", "\n".join(lines))

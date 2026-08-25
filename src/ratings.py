"""注册用户的评分读写（`user_rating`）。游客的等价物是浏览器 localStorage。

🚨 **本模块存的是「作答选项」，不是算好的分数与置信度。**
   `(choice='wish')` 而不是 `(score=8.0, confidence=0.5)` ——
   映射只在 `questionnaire.to_rating()` 一处维护。把映射结果存进库
   等于把它固化在数据里：将来调 `WISH_CONFIDENCE` 就得重写全表，
   而且历史行会带着旧口径静默污染推荐质量。
   📌 这与「前端传 choice 不传分数」是同一条纪律的第三次应用
      （schemas.Answer 的注释 → sql/010 的列注释 → 这里）。

⚠️ **推荐链路不知道评分来自哪里。** 它拿到的是 `list[Answer]`，
   来自 localStorage 还是本表由上一层决定 —— 这正是第 2 节那条
   「评分随请求传入」铁律的兑现，加账号只是多一个数据源。

⚠️ 「没看过」(skip) **不写行**，用缺失表示（设计文档 §4）。
   所以 `upsert_many` 收到 skip 会**删除**已有行 —— 用户把
   「看过」改回「跳过」时，那条旧评分必须消失，不能留在库里。
"""

from dataclasses import dataclass

import psycopg

# 与 questionnaire.to_rating() 认识的选项一致，skip 除外（skip 不写行）。
STORED_CHOICES = ("seen", "wish", "pass")


@dataclass
class Rating:
    subject_id: int
    choice: str
    score: float | None
    source: str


def list_for_user(conn: psycopg.Connection, user_id: int) -> list[Rating]:
    """该用户的全部评分。行数量级是几十~几百，不分页。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT subject_id, choice, score, source FROM user_rating "
            "WHERE user_id = %s ORDER BY subject_id",
            (user_id,),
        )
        return [Rating(*r) for r in cur.fetchall()]


def rated_ids(conn: psycopg.Connection, user_id: int) -> set[int]:
    """已评分的 subject_id —— 直接喂给 `questionnaire.select_items(exclude=)`。

    📌 这就是设计文档「第 6 周要做的三件事」里的第 1 件，
       选题逻辑 2026-08-14 就已经为它准备好了，这里不用改任何算法。
    ⚠️ 只回 `user_rating` 里真有行的 —— skip 不写行，所以「跳过」的题
       下轮还会再出现。**这是有意的**：用户当时没看过，过一阵可能就看了。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT subject_id FROM user_rating WHERE user_id = %s",
                    (user_id,))
        return {r[0] for r in cur.fetchall()}


def upsert_many(conn: psycopg.Connection, user_id: int,
                items: list[tuple[int, str, float | None, str]]) -> tuple[int, int]:
    """批量写入评分。返回 (写入数, 删除数)。

    `items` 每项是 `(subject_id, choice, score, source)`。
    `choice='skip'` 会**删除**该行（见模块注释第三条）。

    ⚠️ **走 upsert 不是 append**：主键 `(user_id, subject_id)` 保证同一部番
       只留最新分。一个用户会多次作答问卷、反复碰到同一部番，append
       的话库里会有多条互相矛盾的分数，取出来时不知道该信哪条。
    ⚠️ `created_at` 在冲突时**保留原值**（`EXCLUDED` 不覆盖它），
       只更新 `updated_at` —— 「第一次评这部番是什么时候」是会丢的信息，
       而丢了就找不回来。
    """
    to_write = [(user_id, sid, ch, sc, src)
                for sid, ch, sc, src in items if ch in STORED_CHOICES]
    to_drop = [sid for sid, ch, _, _ in items if ch not in STORED_CHOICES]

    with conn.cursor() as cur:
        if to_write:
            cur.executemany(
                """
                INSERT INTO user_rating
                    (user_id, subject_id, choice, score, source)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, subject_id) DO UPDATE SET
                    choice     = EXCLUDED.choice,
                    score      = EXCLUDED.score,
                    source     = EXCLUDED.source,
                    updated_at = now()
                """,
                to_write,
            )
        if to_drop:
            cur.execute(
                "DELETE FROM user_rating WHERE user_id = %s AND subject_id = ANY(%s)",
                (user_id, to_drop),
            )
    return len(to_write), len(to_drop)


def delete_all(conn: psycopg.Connection, user_id: int) -> int:
    """清空该用户的评分（前端「清空重答」）。返回删除行数。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM user_rating WHERE user_id = %s", (user_id,))
        return cur.rowcount


def merge_guest(conn: psycopg.Connection, user_id: int,
                items: list[tuple[int, str, float | None, str]]) -> int:
    """游客转正：把 localStorage 的评分并进账号。返回真正写入的行数。

    🚨 **云端为准，本地只补空缺**（Kevin 2026-08-24 定，`DO NOTHING`）。
       理由：账号是跨设备的事实来源，而 localStorage 可能是很久以前
       在某台机器上留下的。让本地覆盖云端，等于用旧数据洗掉新数据，
       且用户完全看不出来发生了什么。
    ⚠️ 所以这里**不能复用 `upsert_many`** —— 那个是 DO UPDATE（覆盖），
       语义正好相反。两个函数长得像但不能合并，各自的注释说明了为什么。
    """
    rows = [(user_id, sid, ch, sc, src)
            for sid, ch, sc, src in items if ch in STORED_CHOICES]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO user_rating (user_id, subject_id, choice, score, source)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id, subject_id) DO NOTHING
            """,
            rows,
        )
        # ⚠️ executemany 的 rowcount 在 psycopg3 里是各批之和，可信；
        #    但为了不依赖驱动细节，这里直接回「尝试写入的条数」的实际生效数。
        return cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)


@dataclass
class RatedDetail:
    """一条评分 + 它指向的那部作品的展示字段（个人页列表用）。

    ⚠️ 与 `Rating` **有意分成两个类型，不要把展示字段并进 `Rating`**：
       后者的形状要与 `/recommend` 的请求体保持一致（打分链路只认
       subject_id/choice/score），多带几列会诱使调用方把它整个转发出去，
       而那正是「评分随请求传入，推荐链路不知道评分从哪来」要避免的耦合。
    """

    subject_id: int
    choice: str
    score: float | None
    source: str
    updated_at: str
    name: str
    name_cn: str | None
    air_year: int | None
    form: str | None
    fav_done: int | None
    bgm_score: float | None


def list_detailed(conn: psycopg.Connection, user_id: int) -> list[RatedDetail]:
    """该用户的评分 + 作品展示字段，按最近修改倒序。

    ⚠️ **INNER JOIN 是安全的**：`user_rating.subject_id` 有指向
       `anime_profile` 的外键（sql/010），孤儿行在库层面就不可能存在。
       📌 那条外键是 `ON DELETE CASCADE`，所以季度更新若真的删掉某部作品，
          相关评分会跟着消失 —— 设计如此（评分指向一部不存在的作品没有意义），
          记在这里免得将来查「用户的评分怎么少了几条」。

    📌 **不分页**：一个用户的评分量级是几十~几百（与 `list_for_user` 同一条
       判断），前端一次拿全再本地筛选，比翻页交互简单得多。

    ⚠️ 排序键带 `subject_id` 兜底：`updated_at` 会大量并列（一次问卷里
       连答几十题落在同一批 UPDATE 上），只按它排的话 Postgres 对并列给
       任意顺序，用户每次刷新看到的列表顺序都不一样。
       📌 与 `recommend_sql` 那条「ORDER BY 必须带次级键 subject_id」同源。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.subject_id, r.choice, r.score, r.source, r.updated_at,
                   a.name, a.name_cn, a.air_year, a.form, a.fav_done,
                   a.score AS bgm_score
              FROM user_rating r
              JOIN anime_profile a USING (subject_id)
             WHERE r.user_id = %s
             ORDER BY r.updated_at DESC, r.subject_id
            """,
            (user_id,),
        )
        return [
            RatedDetail(
                subject_id=sid, choice=ch, score=sc, source=src,
                updated_at=upd.isoformat(),
                name=name, name_cn=name_cn, air_year=year, form=form,
                fav_done=done, bgm_score=bgm,
            )
            for (sid, ch, sc, src, upd, name, name_cn, year, form, done, bgm)
            in cur.fetchall()
        ]

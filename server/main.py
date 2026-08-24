"""FastAPI 应用 —— P0 推荐链路的 HTTP 外壳。

四个接口，**全部无状态**（第 2 节架构铁律）：评分随请求传入，
服务端不存任何用户数据。游客的 localStorage 与第 6 周注册用户的
user_rating 表都往这同一个入口喂数据，推荐链路一行不改。

⚠️ **本模块不持有 Catalog。**（2026-08-12 改）打分走 src/recommend_sql，
   余弦由 pgvector 在库里算。原因见 sql/002_tag_vec.sql 的模块注释：
   目标部署形态是 Vercel serverless，无状态函数里没有常驻进程，
   14 MB 的 tag 矩阵不可能跨请求存活，每请求重建要 2.6 MB / 1.3 s。
   recommend.build_catalog() 仍然保留 —— 第 5 周离线评测要用它跑批量打分。
   两条路径的等价性由 tests/test_parity.py 保证。

⚠️ 端点一律写成 `def` 而不是 `async def`。
   psycopg 是同步的、numpy 会占 GIL —— 写成 async 会阻塞事件循环，
   把并发拖成串行。同步端点由 FastAPI 丢进线程池，才是正确做法。
"""

import datetime
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

from server import schemas
from src import (
    db,
    embed,
    find,
    llm,
    questionnaire,
    recommend,
    recommend_sql,
    related,
    retrieve,
    router,
    tag_rules,
    voice,
)
from src.textproc import dict_fingerprint, keep_tags, norm_name, tokenize

# 建库时的分词词典指纹。search_tsv 是用这套词典切出来的，查询端必须一致 ——
# 不一致意味着查询词切出来和库里对不上，召回静默地崩掉（不报错，只是搜不到）。
# ⚠️ 改 tag 词表或 tokenize() 后必须重跑 load_profiles.py 并更新这个常量。
BUILD_FINGERPRINT = "6a1cbbe1bc4f446d"

# trgm 兜底的相似度下限。实测 'fate zeero' → Fate/Zero 是 0.727，
# 0.3 以下基本是噪声。alias 表没建 trgm 索引（第 1 周为省 9.9 MB 砍掉的），
# 3.8 万行顺序扫描算 similarity 约 90 ms，对一个很少触发的兜底够用。
TRGM_MIN_SIM = 0.3
SEARCH_LIMIT_MAX = 50

_lock = threading.Lock()
_pool: ConnectionPool | None = None
# 问卷选题只取决于库内容与几个参数，整份缓存。模块级变量在同一个函数
# 实例（容器）内跨请求存活；容器被回收就重建，不影响正确性。
# ⚠️ 缓存的是**候选序列**不是「前 n 题」—— 多次作答要按 per-user 的排除集
#    往下顺延，把排除集放进缓存键会让键空间爆炸。
_questions: dict[tuple, list[schemas.QuestionItem]] = {}

# 候选序列缓存多少条。⚠️ 决定了多次作答最多能撑几轮：600 条 ÷ 30 题 = 20 轮，
#    而实测第 6 轮（位次 151-180）的中位热度仍有 18,462，够用了。
#    再大没意义 —— 位次越靠后越冷门，用户没看过就问不出信息。
_POOL_CACHE = 600


def _get_pool() -> ConnectionPool:
    """惰性建池。

    ⚠️ **不放在 lifespan 里。** serverless 平台不保证执行 ASGI lifespan 事件，
       放那里的话线上会拿到一个 None 池，而本地 uvicorn 一切正常 ——
       典型的「本地好好的，上线就挂」。惰性初始化两边都成立。
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:                  # 双检：并发首请求只建一次
                fp = dict_fingerprint()
                if fp != BUILD_FINGERPRINT:
                    # 宁可启动失败也别静默降级 —— 见 textproc.dict_fingerprint()
                    raise RuntimeError(
                        f"分词词典漂移：当前 {fp}，建库时 {BUILD_FINGERPRINT}。"
                        "search_tsv 是用旧词典切的，检索召回已不可信。"
                        "请重跑 scripts/load_profiles.py 并更新 BUILD_FINGERPRINT。")
                _pool = ConnectionPool(
                    db.pool_dsn(), min_size=0, max_size=4, open=True, timeout=30,
                    # ⚠️ Neon 的 -pooler 是 PgBouncer transaction 模式，
                    #    psycopg3 的 prepared statement 会跨事务失效
                    kwargs={"prepare_threshold": None},
                    # ⚠️ 每条新连接都要注册 pgvector 适配器，否则 tag_vec
                    #    读回来是字符串，且 numpy 数组没法当查询参数
                    configure=db.prepare,
                    # 🚨 **借出前必须验活，否则池子会把已死的连接发出去。**
                    #    Neon 是 serverless，空闲连接会被它回收，而 psycopg_pool
                    #    自己不知道 —— 下一个请求拿到它就炸：
                    #      psycopg.OperationalError:
                    #        SSL connection has been closed unexpectedly
                    #    实测复现（2026-08-19）：开着服务打开 /api/docs 看几分钟，
                    #    第一次 POST /api/ask 直接 500，紧接着重试就 200。
                    #    ⚠️ **影响所有端点，不只是 /ask** —— 这是第 2 周就埋下的。
                    #    线上少见只是因为流量近乎为零、几乎每个请求都落在新容器上；
                    #    真正的触发条件是「容器还活着但连接已被回收」，
                    #    也就是两次访问间隔超过 Neon 的空闲窗口。
                    #
                    #    ⚠️ 代价是每次借出多一次往返（一条空查询）。函数与 Neon
                    #       同区（iad1 ↔ us-east-2），实测 RTT 约 15 ms，
                    #       而 /recommend 端到端 235 ms 里 ~200 ms 本来就是
                    #       国内到边缘的 RTT —— 15 ms 换掉一个 500 值得。
                    check=ConnectionPool.check_connection,
                    # 主动比 Neon 更早回收空闲连接，让上面那条 check 尽量不被触发
                    #（check 失败时池会丢弃并重开，能正确恢复，只是慢一次）。
                    max_idle=180.0,
                )
    return _pool


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    with _get_pool().connection() as c:
        yield c


app = FastAPI(
    title="动画推荐 API",
    version="0.3.0",
    description="基于偏好问卷的当季新番推荐（P0：tag 余弦 + mean-centered）",
    # 全部接口挂在 /api 下。前端与 API 同属一个 Vercel 项目、共用一个域名，
    # vercel.json 把 /api/* 转给这个函数、其余交给前端静态产物。
    # ⚠️ 前缀是**同源部署的前提** —— 没有它就没法把静态页面和 API 分流。
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url="/api/redoc",
)
API = "/api"
NL_ = chr(10)

# CORS 仅供本地开发：Vite dev server 在 5173，而 API 跑在 8000，属跨源。
# ⚠️ **线上同源，这段不生效也不需要生效。**
#    别因为「线上出了跨域问题」就来改这里 —— 那说明前端把请求打到了
#    别的域名上（比如硬编码了 API 域名），该改的是前端的 baseURL。
# ⚠️ 仍然不写 ["*"]：第 6 周若把 JWT 放 httpOnly cookie，"*" 与 credentials 互斥。
_origins = os.environ.get(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get(f"{API}/health")
def health() -> dict:
    """存活探针。也顺带确认库里的向量列已回填 —— 忘了跑
    scripts/build_tag_vectors.py 的话打分会静默返回空列表。

    ⚠️ **三条打分路径各有一列，三个都要报。** 原先只报 with_tag_vec，
       而 P1 之后 match 是 tag/embedding/staff 三路融合 —— 漏跑
       build_embeddings.py 或 build_staff_vectors.py 时，那两路会**整项跳过**
       （零向量参与融合会稀释另外两路，所以代码是有意跳过的），
       结果依然像模像样，只是悄悄退化成了纯 tag 模型。
    ⚠️ plot_chunk 那两个是 /api/ask 的前置：没有 vec 就检索不出东西，
       而 G.4 状态③ 会把它报成「这部作品没有语料」—— 症状和"忘了灌库"一样。
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT count(*), count(tag_vec), count(vec), count(staff_vec)
              FROM anime_profile
        """)
        total, with_tag, with_emb, with_staff = cur.fetchone()
        cur.execute("SELECT count(*), count(vec) FROM plot_chunk")
        chunks, chunks_vec = cur.fetchone()
    return {
        "status": "ok" if with_tag else "tag_vec 未回填",
        "catalog_size": total,
        "with_tag_vec": with_tag,
        "with_embed_vec": with_emb,
        "with_staff_vec": with_staff,
        "plot_chunks": chunks,
        "plot_chunks_with_vec": chunks_vec,
        "dict_fingerprint": dict_fingerprint(),
    }


@app.get(f"{API}/questionnaire", response_model=schemas.QuestionnaireResponse)
def get_questionnaire(
    n: int = Query(default=30, ge=1, le=100),
    include_nsfw: bool = False,
    fold_sequels: bool = True,
    experience: schemas.Experience = "veteran",
    exclude: str = Query(default="", description="已评分的 subject_id，逗号分隔"),
) -> schemas.QuestionnaireResponse:
    """选题。第 9 节要求「超发」：要 10 条有效评分就展示 25–30 部。

    `exclude` 支持**多次作答**：把已评分的 id 传进来，跳过它们继续往下取题。
    ⚠️ 游客传 localStorage 里的，注册用户传 `user_rating` 里的 —— 服务端不区分
       （第 2 节架构铁律）。
    """
    try:
        skip = {int(x) for x in exclude.split(",") if x.strip()}
    except ValueError:
        raise HTTPException(422, "exclude 必须是逗号分隔的整数") from None

    # ⚠️ **缓存的是「候选序列」而不是「前 n 题」。** 排除集是 per-user 的，
    #    放进缓存键会让键空间爆炸；而候选序列只由这三个参数决定，可以复用。
    #    顺带把 n 从键里去掉了 —— 原先每个 n 各占一个条目，纯属浪费。
    key = (include_nsfw, fold_sequels, experience)
    if key not in _questions:
        with _conn() as c:
            items = questionnaire.select_items(
                c, n=_POOL_CACHE, include_nsfw=include_nsfw,
                fold_sequels=fold_sequels, experience=experience,
            )
        _questions[key] = [
            schemas.QuestionItem(
                subject_id=it.subject_id, name=it.name, year=it.year,
                done=it.done, form=it.form, replaced_from=it.replaced_from,
            )
            for it in items
        ]
    items = [q for q in _questions[key] if q.subject_id not in skip][:n]

    # ⚠️ 缓存只有 _POOL_CACHE 条，而库里有 4,439 条可问。答满缓存的用户
    #    会拿到**空问卷** —— 200 + total=0，没有任何提示，而实际上还有几千部
    #    可问。这是静默失败，所以缓存耗尽时退回查库（只有答过 ~20 轮才会触发）。
    if len(items) < n:
        with _conn() as c:
            fresh = questionnaire.select_items(
                c, n=len(skip) + n, include_nsfw=include_nsfw,
                fold_sequels=fold_sequels, experience=experience, exclude=skip,
            )
        items = [
            schemas.QuestionItem(
                subject_id=it.subject_id, name=it.name, year=it.year,
                done=it.done, form=it.form, replaced_from=it.replaced_from,
            )
            for it in fresh
        ][:n]

    return schemas.QuestionnaireResponse(
        items=items, experience=experience, total=len(items)
    )


def _weights_of(req: schemas.RecommendRequest) -> recommend.Weights | None:
    """请求里的融合权重 → Weights；三个都没给则返回 None（用服务端默认值）。

    ⚠️ schemas 的 model_validator 已保证「三个一起给或一起不给」，
       所以这里只需看其中一个是不是 None。
    """
    if req.w_tag is None:
        return None
    return recommend.Weights(tag=req.w_tag, emb=req.w_emb, staff=req.w_staff)


def _to_ratings(answers: list[schemas.Answer]) -> list[recommend_sql.Rating]:
    """作答 → Rating。分数/置信度的映射全部委托给 to_rating()。"""
    out: list[recommend_sql.Rating] = []
    for a in answers:
        try:
            t = questionnaire.to_rating(a.choice, a.score)
        except ValueError as e:                     # choice='seen' 却没带分数
            raise HTTPException(422, f"subject_id={a.subject_id}: {e}") from e
        if t is None:                               # skip：用缺失表示，不占位
            continue
        out.append(recommend_sql.Rating(a.subject_id, t[0], t[1], t[2]))
    return out


@app.post(f"{API}/recommend", response_model=schemas.RecommendResponse)
def post_recommend(req: schemas.RecommendRequest) -> schemas.RecommendResponse:
    """无状态打分。评分随请求传入，服务端零写入。

    三次往返：取被评作品的向量 → 算余弦+过滤+召回 → 取 top_k 的向量生成理由。
    第三次只查最终返回的那 20 部，不查整个召回池。
    """
    ratings = _to_ratings(req.answers)
    if not ratings:
        # 空作答不是错误 —— 用户可能刚打开问卷。返回空列表让前端提示继续答题。
        return schemas.RecommendResponse(items=[], used_ratings=0,
                                         rank_by=req.rank_by)

    with _conn() as c:
        # 只算一次偏好向量，打分与推荐理由共用 —— 否则要多一次往返。
        # ⚠️ P1 之后是三元组 (tag, emb, staff)：只传 tag 的话 score() 会为
        #    另外两路再查一次库，`/recommend` 就从 3 次往返变成 4 次。
        prefs = recommend_sql.preference_vectors(c, ratings)
        pref = prefs[0]                      # explain() 只解释 tag 维度
        recs = recommend_sql.score(
            c, ratings, prefs=prefs, mode=req.mode, year_min=req.year_min,
            year_max=req.year_max, include_nsfw=req.include_nsfw,
            min_score=req.min_score, fold_series=req.fold_series,
            rank_by=req.rank_by, blend_alpha=req.blend_alpha, top_k=req.top_k,
            weights=_weights_of(req),
        )
        if not recs:
            return schemas.RecommendResponse(items=[],
                                             used_ratings=len(ratings),
                                             rank_by=req.rank_by)
        ids = [r.subject_id for r in recs]
        norm = pref / (float((pref @ pref) ** 0.5) or 1.0)
        reasons = recommend_sql.explain(c, norm, ids)
        with c.cursor() as cur:
            cur.execute("SELECT subject_id, air_year, score, fav_done "
                        "FROM anime_profile WHERE subject_id = ANY(%s)", (ids,))
            meta = {r[0]: r for r in cur.fetchall()}

    items = [
        schemas.Recommendation(
            subject_id=r.subject_id, name=r.name,
            year=meta[r.subject_id][1],
            match=r.match, quality=r.quality, rank_score=r.rank_score,
            bgm_score=(float(meta[r.subject_id][2])
                       if meta[r.subject_id][2] else None),
            done=meta[r.subject_id][3],
            reasons=reasons.get(r.subject_id, []),
        )
        for r in recs
    ]
    return schemas.RecommendResponse(items=items, used_ratings=len(ratings),
                                     rank_by=req.rank_by)


_SEARCH_COLS = """
    p.subject_id, p.name, p.name_cn, p.air_year, p.form, p.fav_done, p.score
"""


@app.get(f"{API}/search", response_model=list[schemas.SearchHit])
def search(q: str = Query(min_length=1, max_length=100),
           limit: int = Query(default=20, ge=1, le=SEARCH_LIMIT_MAX)
           ) -> list[schemas.SearchHit]:
    """按名字/别名搜作品，供用户主动打分用。

    两级：先 BM25（search_tsv，jieba 预分词），空结果时退到 pg_trgm 模糊匹配。
    ⚠️ 查询端必须走 textproc.tokenize() —— 与建库同一个分词器同一套词典，
       否则「进击的巨人」切出来和库里对不上，一条都搜不到。
    """
    tokens = tokenize(q)
    hits: list[schemas.SearchHit] = []
    with _conn() as c, c.cursor() as cur:
        if tokens:
            cur.execute(f"""
                SELECT {_SEARCH_COLS}, ts_rank(p.search_tsv, qq) AS rk
                FROM anime_profile p, plainto_tsquery('simple', %s) qq
                WHERE p.search_tsv @@ qq
                ORDER BY rk DESC, p.fav_done DESC
                LIMIT %s
            """, (tokens, limit))
            hits = [_hit(r, "tsv") for r in cur.fetchall()]

        if not hits:
            # 兜底：名字拼错/简写。DISTINCT ON 去重 —— 一部作品有多条别名，
            # 不去重的话「Fate/Zero」会因为中日英三个别名各命中一次而刷屏。
            cur.execute(f"""
                SELECT DISTINCT ON (p.subject_id) {_SEARCH_COLS},
                       similarity(a.norm_name, %s) AS sim
                FROM alias a
                JOIN anime_profile p ON p.subject_id = a.subject_id
                WHERE a.entity_type = 'subject'
                  AND similarity(a.norm_name, %s) > %s
                ORDER BY p.subject_id, sim DESC
                LIMIT %s
            """, (norm_name(q), norm_name(q), TRGM_MIN_SIM, limit))
            rows = cur.fetchall()
            # DISTINCT ON 强制按 subject_id 排序，相关性排序只能在 Python 侧补
            rows.sort(key=lambda r: (-r[7], -r[5]))
            hits = [_hit(r, "trgm") for r in rows]
    return hits


def _hit(row: tuple, via: str) -> schemas.SearchHit:
    sid, name, name_cn, year, form, done, sc = row[:7]
    return schemas.SearchHit(
        subject_id=sid, name=name, name_cn=name_cn, year=year, form=form,
        done=done, bgm_score=float(sc) if sc else None, via=via,
    )


@app.get(API + "/anime/{subject_id}", response_model=schemas.AnimeDetail)
def anime_detail(subject_id: int) -> schemas.AnimeDetail:
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT subject_id, name, name_cn, name_en, summary, air_date,
                   air_year, form, nsfw, score, score_count, rank, fav_done,
                   tags, meta_tags, studios, anilist_id
            FROM anime_profile WHERE subject_id = %s
        """, (subject_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, f"没有 subject_id={subject_id} 的作品")

    (sid, name, name_cn, name_en, summary, air_date, air_year, form, nsfw,
     sc, sc_cnt, rank, done, tags, metas, studios, anilist_id) = row

    # 只回落在词表内的题材 tag —— 与推荐用的向量维度保持同一口径。
    # 直接回原始 tags 会把制作公司/声优/年份也吐给前端，那不是题材信息。
    vocab = keep_tags()
    clean = [t["name"] for t in (tags or [])
             if tag_rules.normalize(t["name"]) in vocab
             and tag_rules.classify(tag_rules.normalize(t["name"])) == "KEEP"]

    return schemas.AnimeDetail(
        subject_id=sid, name=name, name_cn=name_cn, name_en=name_en,
        summary=summary, air_date=air_date.isoformat() if air_date else None,
        air_year=air_year, form=form, nsfw=nsfw,
        bgm_score=float(sc) if sc else None, score_count=sc_cnt, rank=rank,
        done=done, tags=clean, meta_tags=list(metas or []),
        studios=list(studios or []), anilist_id=anilist_id,
    )


def _off_topic_response(reason: str) -> schemas.AskResponse:
    """`llm.classify_intent()` 判"这不是动画相关的问题"时的统一回复。

    ⚠️ 与 `find_gate()` 那句"描述有些模糊不清"是**两种不同的失败**，
    不要合并成一句：这里是"这似乎不是本系统能回答的问题"，
    find_gate 那句是"是找番但描述不够具体"——两条失败信息混在一起，
    用户会分不清自己该怎么改问法。
    """
    return schemas.AskResponse(
        route="ask", route_reason=reason, state="unknown",
        answer="这似乎不是关于动画的问题呢，要不要换个和番剧、角色相关的问法？",
        series_root=None, title=None, chunks=[], candidates=[],
        meta={"intent": "off_topic"},
    )


def _find_response(route: str, reason: str, query: str,
                   hits: list[find.FindHit]) -> schemas.AskResponse:
    """把找番结果包成 AskResponse。**零 LLM 调用**——文案是代码拼接的列表，
    不是生成的，与 season 分支「head + 拼行」同一套做法（不给 LLM 开
    第二条"编排展示文案"的口子，展示层就该是纯拼接）。

    被两处复用：按钮强制 `route=find`，以及 ask 分支 resolve() 完全没认出
    任何作品时的最后一步兜底（见 ask() 内的调用点）。

    ⚠️ **措辞刻意不说"找到了"——只说"语义上接近"。** 语义检索对任何输入
       都能返回一个 top-k（cos 排序不存在"零结果"），而实测两者的余弦分布
       **有重叠**：离题查询「今天天气怎么样」top1=0.540、「晚饭吃什么好」
       top1=0.500，都高于/接近在题查询的下界；样本太小（n=7 随手测的）不足以
       标定一个可靠的绝对地板——与 B.4 那次「rerank 分噪声 ~1e-3，绝对地板
       在物理上调不稳」同一类坑，**不要在证据不足时先编一个数字**。
       ⇒ 用词软化到"仅供参考"代替设阈值：宁可对真正在题的查询也谦虚一点，
         也不要对离题查询显得胸有成竹——「自信地答错很贵」同一条纪律。
    ⬜ 真要做置信地板，需要一批标注过"在题/离题"的查询做 A/B，量出分布
       是否可分——目前只有本条 docstring 里这 7 条随手测的数，不够。
    """
    lines = [f"{h.name}（{h.air_year}）" if h.air_year else h.name for h in hits]
    return schemas.AskResponse(
        route="find", route_reason=reason, state="ok",
        answer=("没有认出你问的是哪部具体作品，语义上比较接近的番（仅供参考，"
                "不保证真的是你要找的）：" + NL_.join([""] + lines)),
        series_root=None, title=None, chunks=[], candidates=[],
        # ⚠️ 不写 zero_model —— 这条分支调了一次 embedding，
        #    与 voice/season 那两条**真正**零模型的分支不是一回事。
        meta={"embed_only": True},
        find=schemas.FindResponse(
            query=query,
            items=[schemas.FindHit(subject_id=h.subject_id, name=h.name,
                                   air_year=h.air_year, match=h.match)
                  for h in hits],
        ),
    )


@app.post(f"{API}/ask", response_model=schemas.AskResponse)
def ask(req: schemas.AskRequest) -> schemas.AskResponse:
    """流程 C · 剧情问答。四步管道见 src/retrieve.py 的模块注释。

    ⚠️ **这是请求路径上第一个会调模型的端点**（前三周的推荐链路全程零模型）。
       一次请求打三个外部服务：embedding → rerank → LLM。
       **实测端到端 10–45 秒**（2026-08-23 抽样 7 道，此前记的「3–6 秒」已不成立）。
       分层实测（安兹那道，19.8 s）：
           resolve/pinned/recall (SQL)   0.59 s    3.0%
           embed_query (API)             1.68 s    8.5%
           rerank (API, 53 条)            2.01 s   10.1%
           LLM 生成 371 字               15.54 s   78.4%   ← 瓶颈
       ⇒ **检索层一共才 4.3 秒，优化它没有意义**；要快只能上流式输出。
       ⇒ 与 /recommend 的 235 ms 不是一个量级，前端必须给 loading 态。
    📌 voice / season 两条分支**不调模型**，实测 0.3–0.4 秒。

    ⚠️ **四种状态都返回 200，不要把它们做成 4xx。** 它们是正常的业务结果
       （反问 / 没语料 / 没认出），不是错误：G.4 明确要求「认出来但没语料」时
       短路返回而不是把问题丢给 LLM —— 那会让它用训练记忆流畅编一个出来，
       绕过整条 RAG 链路（没有出处、没有剧透门控、可能是幻觉）。
       把它做成 404 的话，前端很容易顺手退化成「那就直接问模型吧」。

    ⚠️ **失败要分清是哪一层**，因为降级方向完全不同（A.8）：
         embedding 挂  → 整条向量检索没了，正确兜底是退回纯 BM25 → 503
         rerank 挂     → retrieve() 内部已降级成向量序，请求照常成功
         LLM 挂        → 检索结果是好的，只是没法生成 → 503，但把 chunks 带回去
    """
    # ── 分派 ──────────────────────────────────────────────────
    # ⚠️ 这是**意图路由**不是意图分类器：信号全部确定性（触发词 + 时间正则），
    #    没有一处交给模型判。见 src/router.py 的模块注释。
    if req.route == "auto":
        route, reason = router.classify(req.question)
    else:
        route, reason = req.route, "由调用方指定"
    fell_back = ""          # 非空 = 从 voice/season/find 回落过来的，原因写在里面

    # ── 意图校验（2026-08-24 加，Kevin 定）：整个请求只调一次 ────────
    # 🚨 起因是实测发现除 voice 外的按钮分支会"自信地答非所问"——season
    #    按钮问剧情问题会无视问句直接展示当季新番列表，find 按钮问声优问题
    #    会硬凑一堆题材相近的番，两者都没有 voice 那种"找不到就回落"的天然
    #    信号。同时发现自动分派下 `resolve()` 会被短别名字符串巧合命中
    #    （"今天心情不好"→"今**天心**情"撞上角色「天心」），把纯闲聊答成
    #    了某个角色的问答——这一层完全在模型之前，`route.classify()` 那套
    #    触发词规则也管不到。⇒ 补一道 `llm.classify_intent()`（Qwen/Qwen3-8B，
    #    与主力问答模型分开，成本可忽略），覆盖自动分派与按钮强制两种情况
    #    （Kevin 定："auto + 所有按钮都校验"）。
    # ⚠️ **auto 模式下直接采信/改判，按钮模式下只用来"拦不对的"，不用来
    #    "静默改判到另一个具体分支"**：自动分派本来就是猜的，用更细的判断
    #    替换它没有额外代价；但按钮是用户的显式指令，被一次不保证对的分类
    #    静默改判到别的分支，比"退回通用问答"风险更高——`voice.find_person()`
    #    的自身空结果检测已经证明这一点：实测"不存在的声优xyzq"被
    #    classify_intent 误判成 off_topic，若拿它去覆盖 voice 分支的空结果
    #    回落，会把原本更准确的"没有找到这个名字的声优"换成一句更含糊的
    #    离题提示，是纯倒退（2026-08-24 实测抓到并改回）。⇒ **voice 分支的
    #    空结果回落完全不看 intent**，season/find 因为没有等价的自身空结果
    #    信号，才需要在分支入口处直接用 intent 校验。
    try:
        intent, _served = llm.classify_intent(req.question)
    except llm.LLMError:
        intent = None       # 校验挂了：信任原路由，不因为这道关失败而拒绝服务

    if req.route == "auto":
        if intent == "off_topic":
            return _off_topic_response("意图判断认为这不是动画相关的问题")
        if intent is not None and intent != route:
            # router.classify() 只会猜 voice/season/ask 三种，从不会猜 find——
            # LLM 的判断更细，直接采用（这正是 [7] 类问题在 resolve() 之前
            # 就被挡下来的位置）。
            route, reason = intent, f"LLM 判断意图为 {intent}"

    with _conn() as c:
        # ── voice：某声优配过哪些角色。零模型（intent 校验除外）。────
        if route == "voice":
            # ⚠️ 按钮路径用**宽松判据**：用户点了「声优」就是显式指令，
            #    不需要任何触发词 —— 「花泽香菜」这类裸名字正是自动分派
            #    覆盖不到、而按钮能救的那一类（router.relax_voice 的注释）。
            people = (voice.find_person(c, req.question)
                      if req.route == "voice" or router.relax_voice(req.question)
                      else [])
            if people:
                person = people[0]
                items = voice.roles_of(c, person.person_id, limit=req.top_k * 2)
                ctx = voice.as_context(person, items)
                return schemas.AskResponse(
                    route="voice", route_reason=reason, state="ok",
                    answer=ctx[1] if ctx else None,
                    series_root=None, title=None, chunks=[], candidates=[],
                    meta={"zero_model": True},
                    voice=schemas.VoiceResponse(
                        person_id=person.person_id, name=person.name,
                        name_cn=person.name_cn, n_roles=person.n_roles,
                        items=[schemas.VoiceRoleItem(**vars(r)) for r in items],
                    ),
                )
            # ⚠️ 空结果**回落而不是 404** —— 用户点错按钮很常见，
            #    硬失败会让他以为功能坏了。回落要在 route_reason 里说清楚。
            #    🚨 **这里不用 intent 短路 off_topic**（与 season/find 不同）：
            #    "没有找到这个名字的声优"本身就是准确、有信息量的回答，
            #    实测 classify_intent 会把"不存在的声优xyzq"这类带明显人名
            #    形态的乱码误判成 off_topic —— 用一个不保证对的分类结果去
            #    覆盖一个已经算准确的结论，纯属倒退（这条是 2026-08-24 实测
            #    抓到的真实回归，改回这样才修好）。
            fell_back = "没有找到这个名字的声优。"
            route, reason = "ask", "没找到这个声优，改按剧情问答处理"

        # ── season：按档期浏览。零模型（intent 校验除外）。──────────
        if route == "season":
            # ⚠️ 没有 voice 那种"找不到就是空结果"的天然信号——_season_payload
            #    对任何输入都会返回"当季列表"，必须在这里提前拦，不能等它
            #    跑完再判断对不对。
            if intent is not None and intent != "season":
                if intent == "off_topic":
                    return _off_topic_response("意图判断认为这不是动画相关的问题")
                fell_back = "这道题看起来不像是新番/档期类问题。"
                route, reason = "ask", (f"按钮选择是 season，意图判断认为更像 "
                                        f"{intent}，改按剧情问答处理")
            else:
                cour = router.parse_cour(req.question)
                payload = _season_payload(c, *(cour or (None, None)),
                                          limit=req.top_k * 3, include_nsfw=False)
                if payload.items:
                    head = (f"{payload.year} 年 {payload.month} 月番共 "
                            f"{payload.total} 部，按热度列出前 {len(payload.items)} 部：")
                    lines = [f"{it.name_cn or it.name}（{it.air_date}）"
                             for it in payload.items]
                    return schemas.AskResponse(
                        route="season", route_reason=reason, state="ok",
                        answer=head + NL_.join([""] + lines),
                        series_root=None, title=None, chunks=[], candidates=[],
                        meta={"zero_model": True}, season=payload,
                    )
                # ⚠️ **当季/未来天生稀薄**（候选集 done>=50 挡住新番，
                #    「下一季」实测 0 部）—— 这里给话术，不要去放宽档期窗口，
                #    放宽只会把上一季的旧番混进来冒充新番。
                fell_back = (f"{payload.year} 年 {payload.month} 月番在库内没有收录作品"
                             "——新番的收藏数还不够，暂时进不了候选集。")
                route = "ask"
                reason = ("该档期在库内没有收录作品（新番收藏数不足，"
                          "见「季度更新」①），改按剧情问答处理")

        # ── find：流程 B 语义找番（G.1 路径①）。**按钮强制走这里**。────
        # ⚠️ 与 season 同一个理由：语义检索没有天然空结果信号，必须提前拦。
        if route == "find":
            if intent is not None and intent != "find":
                if intent == "off_topic":
                    return _off_topic_response("意图判断认为这不是动画相关的问题")
                fell_back = "这道题看起来不像是找番类问题。"
                route, reason = "ask", (f"按钮选择是 find，意图判断认为更像 "
                                        f"{intent}，改按剧情问答处理")
            else:
                hits = find.find(c, req.question, top_k=req.top_k,
                                 retries=retrieve.REQUEST_EMBED_RETRIES,
                                 timeout=retrieve.REQUEST_EMBED_TIMEOUT)
                if hits:
                    return _find_response("find", "按语义找番查询", req.question, hits)
                fell_back = "没有找到符合描述的番。"
                route, reason = "ask", "找番没有结果，改按剧情问答处理"

        # ── ask 分支自身也要挡 off_topic ──────────────────────────
        # ⚠️ auto 模式在最上面已经短路过了（route 不可能带着 off_topic
        #    走到这里）；这里补的是**显式 route=ask** 且校验认为离题的情况
        #    ——不补的话，「今天心情不好」这类问题在 route=auto 时被挡住，
        #    换成 route=ask 强制却又漏了，前后不一致。
        if route == "ask" and intent == "off_topic" and not fell_back:
            return _off_topic_response("意图判断认为这不是动画相关的问题")

        # ── ask：流程 C 剧情问答（内含 related 补充上下文）──────────
        try:
            res = retrieve.ask(c, req.question,
                               spoiler=req.spoiler, final=req.top_k,
                               scope=req.scope, history=req.history)
        except embed.EmbedError as exc:
            # ⚠️ 不要在这里 fallback 到别的 embedding 模型 —— 库里 89,544 条
            #    向量是 Qwen3 定义的坐标系，换模型不报错、只返回排好序的噪声。
            raise HTTPException(503, f"向量检索不可用：{exc}") from exc
        except llm.LLMError as exc:
            raise HTTPException(503, f"生成不可用：{exc}") from exc

        # ── find：resolve() 完全没认出任何作品/角色时的最后一步兜底 ──────
        # ⚠️ G.1 路径①：「没认出是哪部」本身就是找番的触发条件，不是死路——
        #    第四部分「单一入口」④「以上都不命中 → 流程 B 语义找番」，
        #    只是这一层要等 resolve() 真的查过库才知道该不该触发：
        #    AMBIGUOUS/NO_CORPUS 都已经认出了具体作品，找番帮不上忙，
        #    还会掩盖"你是指哪一部"这个更有用的反问，所以只在 UNKNOWN 触发。
        # ⚠️ **只在 fell_back 为空时触发**——voice/season 回落到这里时已经
        #    带着更具体的失败原因（「没有找到这个名字的声优」之类），找番
        #    在这里插一段语义检索结果反而会把那句更有用的话挤掉，与「回落
        #    原因必须出现在 answer 里」那条纪律冲突。
        # ⚠️ 不额外花一次 embedding——retrieve() 内部非 OK 状态时**从不调
        #    embed_query**（短路早于那一步），这里是本轮请求唯一的一次编码。
        # 🚨 **先过一道 LLM 门控，不直接跑 find()**（Kevin 2026-08-24 定）：
        #    find() 的语义检索对任何输入都会返回一个 top-k（cos 排序不存在
        #    "零结果"），实测离题查询（「今天天气怎么样」）与在题查询的余弦
        #    分数**有重叠**，手调一个绝对地板是 B.4 那个"调不稳"的坑，样本
        #    也不够标定。⇒ 交给 llm.find_gate() 判断这句话是不是一段找番
        #    描述，判"否"就给一句套话请用户说具体点，不跑语义检索。
        if not fell_back and res.state is retrieve.State.UNKNOWN:
            try:
                is_findable, _served = llm.find_gate(req.question)
            except llm.LLMError:
                is_findable = None    # 门控挂了：不猜，退回原来的"没认出"
            if is_findable is True:
                try:
                    hits = find.find(c, req.question, top_k=req.top_k,
                                     retries=retrieve.REQUEST_EMBED_RETRIES,
                                     timeout=retrieve.REQUEST_EMBED_TIMEOUT)
                except embed.EmbedError:
                    hits = []   # 找番也挂了：退回原来的"没认出"，别炸整个请求
                if hits:
                    return _find_response(
                        "find", "resolve() 没认出任何作品，"
                                "LLM 判断这是一段找番描述，改按流程 B 语义找番",
                        req.question, hits)
            elif is_findable is False:
                return schemas.AskResponse(
                    route="ask",
                    route_reason="resolve() 没认出任何作品，"
                                "LLM 判断问题描述不够具体，未触发找番",
                    state="unknown",
                    answer="描述有些模糊不清，能再具体一点吗？"
                          "比如题材、设定、人物关系或者剧情走向。",
                    series_root=None, title=None, chunks=[], candidates=[],
                    meta={"find_gate": False},
                )

        # ⚠️ 候选的年份要在连接还开着的时候取。同名重制版（《多罗罗》1969/2019，
        #    全库 81 例）不带年份就是两个一模一样的选项，用户无从选择。
        roots = {m.series_root: m.title for m in res.resolution.candidates}
        years: dict[int, int | None] = {}
        if roots:
            with c.cursor() as cur:
                cur.execute("SELECT subject_id, air_year FROM anime_profile "
                            "WHERE subject_id = ANY(%s)", (list(roots),))
                years = dict(cur.fetchall())

    return schemas.AskResponse(
        # ⚠️ route 可能不是分派器最初判的那个 —— voice/season 空结果会回落到
        #    这里，此时 reason 里写着为什么回落。回传的必须是**实际走的**。
        route=route, route_reason=reason,
        state=res.state.value,
        # ⚠️ **回落的原因必须出现在 answer 里，不能只放在 route_reason。**
        #    用户主要看 answer —— 实测「下一季有什么新番」回落后只显示
        #    「没认出你问的是哪部作品或哪个角色」，那句话对他毫无信息量，
        #    真正的原因（该档期没收录）却藏在一个他不会看的字段里。
        #    ⚠️ 只在 ask 也没答上来时拼接：ask 真答出东西了就别打断它。
        answer=(f"{fell_back}{res.text or ''}"
                if fell_back and res.state.value != "ok" else res.text),
        series_root=res.resolution.series_root,
        title=res.resolution.title,
        chunks=[
            schemas.AskChunk(
                chunk_id=ch.chunk_id, section=ch.section, text=ch.text,
                kind=ch.kind, source=ch.source,
                spoiler_level=ch.spoiler_level, score=ch.score, pinned=ch.pinned,
            ) for ch in res.chunks
        ],
        # ⚠️ 按 series_root 去重：同一部作品会因为多个同名角色出现多次
        #    （「拉姆」实测撞 4 部，但每部只该在反问里出现一次）。
        # ⚠️ **不能按标题去重** —— 那会把《多罗罗》1969 与 2019 折成一个选项。
        candidates=[
            schemas.AskCandidate(series_root=root, title=title,
                                 year=years.get(root))
            for root, title in sorted(roots.items())
        ],
        meta=res.meta,
    )


@app.get(f"{API}/related", response_model=schemas.RelatedResponse)
def get_related(
    series_root: int = Query(..., description="作品的系列根 id"),
    role: str = Query(
        default="",
        description="要查的岗位，逗号分隔；留空则查 原作+导演+制作公司"),
    limit: int = Query(8, ge=1, le=50),
) -> schemas.RelatedResponse:
    """同作者 / 同导演 / 同制作公司的其他作品。**零模型调用。**

    ⚠️ **这条路径存在的理由是 /ask 答不了这类问题，而且答不了是对的**：
       流程 C 的召回写死了 `WHERE series_root = X`（实体消歧铁律，
       第 15 节原则 4），按设计就看不到别的作品的 chunk。
       实测「冰之城墙的作者还画过其他漫画吗」→「资料中没有提到」，
       而 staff 列里一条 SQL 就查得到《相反的你和我》。

    ⚠️ 人名从**本作品自己的 staff 列**里读出来再反查，不做文本匹配 ——
       staff 存日文原形（阿賀沢紅茶）、萌娘正文用简体（阿贺泽红茶），
       两者字符重合度极低（第 13 节记过 冨樫義博 vs 富坚义博 的同类坑）。

    ⚠️ 结果按 `series_root` 折叠：续作不算「另一部作品」，
       与推荐链路用的是同一个折叠键，不另起一套。
    """
    roles = [r.strip() for r in role.split(",") if r.strip()] or ["原作", "导演"]
    bad = [r for r in roles if r not in related.STAFF_ROLES]
    if bad:
        raise HTTPException(
            422, f"未知岗位 {bad}；库里只有 {list(related.STAFF_ROLES)}")

    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT coalesce(name_cn, name) FROM anime_profile WHERE subject_id = %s",
                (series_root,))
            row = cur.fetchone()
        if row is None:
            raise HTTPException(404, f"没有 series_root={series_root} 的作品")
        items = related.same_staff(c, series_root, roles, limit=limit)
        if not role:                      # 默认模式才顺带查公司
            seen = {r.series_root for r in items}
            items += [r for r in related.same_studio(c, series_root, limit=limit)
                      if r.series_root not in seen]

    return schemas.RelatedResponse(
        series_root=series_root, title=row[0],
        items=[schemas.RelatedWork(**vars(r)) for r in items[:limit]],
    )


@app.get(f"{API}/voice", response_model=schemas.VoiceResponse)
def get_voice(
    name: str = Query(..., min_length=2, description="声优名，中日文均可"),
    limit: int = Query(20, ge=1, le=100),
) -> schemas.VoiceResponse:
    """某位声优配过哪些角色。**零模型调用。**

    ⚠️ **这条路径存在的理由与 /related 相同：/ask 答不了，而且答不了是对的。**
       流程 C 的召回写死了 `WHERE series_root = X`，天生看不到跨作品的事实；
       而钉宫理惠在库里有 478 条配役，靠向量召回最多凑出零星几条提及。

    ⚠️ 名字走 alias 的 norm_name **精确匹配**，不做繁简/变体的模糊匹配 ——
       sql/009 把声优名灌进 alias 正是为此（「花泽香菜」→ 花澤香菜）。
       📌 这些 person 行不会污染作品/角色解析：retrieve.find_mentions 的
          JOIN 条件是 coalesce(parent_subject_id, subject_id)，
          而声优行这两列都是 NULL，INNER JOIN 天然把它们排除。

    ⚠️ 结果按 **character_id** 折叠而不是 series_root：问的是「配过哪些角色」，
       同一角色出现在续作/OVA 里只算一次。选代表作时 role_type 优先于热度 ——
       否则「神乐」会被《齐木楠雄》的客串役顶掉《银魂》的主角役（见 voice.py）。
    """
    with _conn() as c:
        people = voice.find_person(c, name)
        if not people:
            raise HTTPException(404, f"没有找到声优「{name}」")
        p = people[0]
        items = voice.roles_of(c, p.person_id, limit=limit)

    return schemas.VoiceResponse(
        person_id=p.person_id, name=p.name, name_cn=p.name_cn,
        n_roles=p.n_roles,
        items=[schemas.VoiceRoleItem(**vars(r)) for r in items],
    )


@app.get(f"{API}/find", response_model=schemas.FindResponse)
def get_find(
    q: str = Query(min_length=1, max_length=200),
    top_k: int = Query(default=find.TOP_K_DEFAULT, ge=1, le=20),
    include_nsfw: bool = False,
) -> schemas.FindResponse:
    """流程 B · 找番（G.1 路径①）。用一段描述找作品，不需要先知道片名。

    ⚠️ **这是除 /ask 之外唯一会调模型的端点**——只调 embedding 一次，
       不调 rerank/LLM，实测延迟量级与 /search 接近（数百毫秒），不是
       /ask 的 10~45 秒。只用语义腿，不混 BM25——`scripts/eval_find.py`
       实测过 RRF 融合在转述类查询上是净负收益，见 src/find.py 的模块注释。
    """
    with _conn() as c:
        try:
            hits = find.find(c, q, top_k=top_k, include_nsfw=include_nsfw,
                             retries=retrieve.REQUEST_EMBED_RETRIES,
                             timeout=retrieve.REQUEST_EMBED_TIMEOUT)
        except embed.EmbedError as exc:
            raise HTTPException(503, f"向量检索不可用：{exc}") from exc
    return schemas.FindResponse(
        query=q,
        items=[schemas.FindHit(subject_id=h.subject_id, name=h.name,
                               air_year=h.air_year, match=h.match)
              for h in hits],
    )


@app.get(f"{API}/season", response_model=schemas.SeasonResponse)
def season(
    year: int | None = Query(default=None, ge=1890, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    include_nsfw: bool = False,
    limit: int = Query(default=100, ge=1, le=200),
) -> schemas.SeasonResponse:
    """某个季度在播什么。**零模型调用，一条 SQL。**

    year/month 缺省取今天（UTC）所在季度，所以「十年前的这个季度」
    只要传 `year=今年-10` —— 意图路由层（第四部分分支③）就是这么用的。

    ⚠️ **air_date 上没有索引，这是 Seq Scan**（实测 5.9 ms / 扫 11,453 行）。
       不要照抄 /api/related 那句「走索引的 SQL」—— 那条是 staff @> jsonb
       命中 GIN，本条不是。5.9 ms 相对 ~200 ms 的跨国 RTT 是噪声，
       所以**不建索引**；真要建也得先量，别凭「看着像范围查询」就加。
    ⚠️ 不挂在 /recommend 上：那个要传评分才能算偏好向量，而这里是
       无个性化的浏览。窗口口径的唯一定义处是 recommend.cour_window()
       （本季起点 −7 天 ~ 下季起点，右开），不要在 SQL 里另写日期算术。
    ⚠️ 排序按 fav_done 降序 + subject_id 次级键 —— 并列时 Postgres 给
       任意顺序，没有次级键结果不可复现（B 节踩过的同一个坑）。

    ⚠️ **回溯型查询才有料，当季/未来天生稀薄** —— 这不是本端点的 bug，
       是候选集 `done>=50` 的口径（第四部分「季度更新」①）：
         2016 年夏季  134 部        ← 「十年前的这个季度」，主力用法
         当季(2026Q3)  43 部        ← dump 快照最晚 air_date 2026-09-11
         下一季         0 部        ← 库内 air_date > 今天的总共只有 2 部
       与「mode=upcoming 实测只召回 2 部」同一个根因。⇒ 路由层拿它答
       「下季看什么」会返回空列表，**要在那里给话术，不要在这里放宽窗口**。
    """
    with _conn() as c:
        return _season_payload(c, year, month, limit, include_nsfw)


def _season_payload(c, year: int | None, month: int | None,
                    limit: int, include_nsfw: bool) -> schemas.SeasonResponse:
    """档期查询的本体。⚠️ 抽出来是为了让 POST /api/ask 的 season 分支复用 ——
    复制一份的话两条路径迟早对「哪些算这一季」给出不同答案。"""
    today = datetime.datetime.now(datetime.UTC).date()
    lo, hi = recommend.cour_window(year or today.year, month or today.month)

    with c.cursor() as cur:
        cur.execute("""
            SELECT subject_id, name, name_cn, air_date, form, fav_done, score,
                   count(*) OVER () AS total
              FROM anime_profile
             WHERE air_date >= %s AND air_date < %s
               AND (%s OR NOT nsfw)
             ORDER BY fav_done DESC, subject_id
             LIMIT %s
        """, (lo, hi, include_nsfw, limit))
        rows = cur.fetchall()

    # 归一化回季度月：查询传 8 月，结果标 7 月番
    cour_start = lo + datetime.timedelta(days=recommend.COUR_GRACE_DAYS)
    return schemas.SeasonResponse(
        year=cour_start.year, month=cour_start.month,
        window_start=lo.isoformat(), window_end=hi.isoformat(),
        total=rows[0][7] if rows else 0,
        items=[
            schemas.SeasonItem(
                subject_id=r[0], name=r[1], name_cn=r[2],
                air_date=r[3].isoformat(), form=r[4], done=r[5],
                bgm_score=float(r[6]) if r[6] is not None else None,
            ) for r in rows
        ],
    )

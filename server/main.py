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

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

from server import schemas
from src import db, questionnaire, recommend_sql, tag_rules
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
    scripts/build_tag_vectors.py 的话打分会静默返回空列表。"""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*), count(tag_vec) FROM anime_profile")
        total, with_vec = cur.fetchone()
    return {
        "status": "ok" if with_vec else "tag_vec 未回填",
        "catalog_size": total,
        "with_tag_vec": with_vec,
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
    return schemas.QuestionnaireResponse(
        items=items, experience=experience, total=len(items)
    )


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
        # 只算一次偏好向量，打分与推荐理由共用 —— 否则要多一次往返
        pref = recommend_sql.preference_vector(c, ratings)
        recs = recommend_sql.score(
            c, ratings, pref=pref, mode=req.mode, year_min=req.year_min,
            year_max=req.year_max, include_nsfw=req.include_nsfw,
            min_score=req.min_score, fold_series=req.fold_series,
            rank_by=req.rank_by, blend_alpha=req.blend_alpha, top_k=req.top_k,
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

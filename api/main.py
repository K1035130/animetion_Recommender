"""FastAPI 应用 —— P0 推荐链路的 HTTP 外壳。

四个接口，**全部无状态**（第 2 节架构铁律）：评分随请求传入，
服务端不存任何用户数据。游客的 localStorage 与第 6 周注册用户的
user_rating 表都往这同一个入口喂数据，推荐链路一行不改。

⚠️ 端点一律写成 `def` 而不是 `async def`。
   psycopg 是同步的、numpy 矩阵乘法会占满 GIL —— 写成 async 会阻塞事件循环，
   把并发拖成串行。同步端点由 FastAPI 丢进线程池，才是正确做法。
"""

import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager

import numpy as np
import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from psycopg_pool import ConnectionPool

from api import schemas
from src import db, questionnaire, recommend, tag_rules
from src.textproc import dict_fingerprint, keep_tags, norm_name, tokenize

# 建库时的分词词典指纹。search_tsv 是用这套词典切出来的，查询端必须一致 ——
# 不一致意味着查询词切出来和库里对不上，召回静默地崩掉（不报错，只是搜不到）。
# ⚠️ 改 tag 词表或 tokenize() 后必须重跑 load_profiles.py 并更新这个常量。
BUILD_FINGERPRINT = "6a1cbbe1bc4f446d"

# trgm 兜底的相似度下限。实测 'fate zeero' → Fate/Zero 是 0.727，
# 0.3 以下基本是噪声。alias 表没建 trgm 索引（第 1 周为省 9.9 MB 砍掉的），
# 3.8 万行顺序扫描算 similarity 约几十毫秒，对一个很少触发的兜底够用。
TRGM_MIN_SIM = 0.3
SEARCH_LIMIT_MAX = 50

_state: dict = {}


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    with _state["pool"].connection() as c:
        yield c


@asynccontextmanager
async def lifespan(app: FastAPI):
    fp = dict_fingerprint()
    if fp != BUILD_FINGERPRINT:
        # 宁可启动失败也别静默降级 —— 见 textproc.dict_fingerprint() 的注释
        raise RuntimeError(
            f"分词词典漂移：当前 {fp}，建库时 {BUILD_FINGERPRINT}。"
            "search_tsv 是用旧词典切的，检索召回已不可信。"
            "请重跑 scripts/load_profiles.py 并更新 BUILD_FINGERPRINT。"
        )

    # min_size=1：Neon 免费层 scale-to-zero，保持一条常连能免掉后续请求的冷启动。
    # max_size 别开大 —— 免费层连接数有限，且本服务几乎不并发写。
    pool = ConnectionPool(
        db.pool_dsn(), min_size=1, max_size=5, open=True, timeout=30,
        # ⚠️ Neon 的 -pooler 是 PgBouncer transaction 模式，
        #    psycopg3 的 prepared statement 会跨事务失效
        kwargs={"prepare_threshold": None},
    )
    _state["pool"] = pool

    # Catalog 常驻内存：11453×308 float32 = 14 MB，构建要 1.7 s（拉全库 + 清洗 tag）。
    # ⚠️ 绝不能每请求重建 —— 那会把 10 ms 的打分变成 1.7 s。
    with pool.connection() as c:
        _state["catalog"] = recommend.build_catalog(c)
    _state["questions"] = {}
    yield
    pool.close()


app = FastAPI(
    title="动画推荐 API",
    version="0.1.0",
    description="基于偏好问卷的当季新番推荐（P0：tag 余弦 + mean-centered）",
    lifespan=lifespan,
)

# 前端在 Vercel、后端在 Render，属跨站。默认放开本地 Vite 端口，
# 线上用 CORS_ORIGINS 环境变量覆盖（逗号分隔）。
# ⚠️ 不要图省事写 ["*"] —— 第 6 周加 Cookie 认证时 "*" 与 credentials 互斥，
#    到时候要回头改，不如现在就写对。
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


def _catalog() -> recommend.Catalog:
    return _state["catalog"]


@app.get("/health")
def health() -> dict:
    """存活探针。Render 用它判断实例是否就绪。"""
    cat = _catalog()
    return {
        "status": "ok",
        "catalog_size": len(cat.ids),
        "vocab_size": len(cat.vocab),
        "dict_fingerprint": dict_fingerprint(),
    }


@app.get("/questionnaire", response_model=schemas.QuestionnaireResponse)
def get_questionnaire(
    n: int = Query(default=30, ge=1, le=100),
    include_nsfw: bool = False,
    fold_sequels: bool = True,
    experience: schemas.Experience = "veteran",
) -> schemas.QuestionnaireResponse:
    """选题。第 9 节要求「超发」：要 10 条有效评分就展示 25–30 部。

    结果只取决于库内容与这几个参数，因此可以整份缓存 ——
    否则每次进首页都要全表扫 11k 行。第 5–6 周换成聚类选代表时，
    这层缓存同样适用（那时更该缓存，k-means 不可能每请求跑一遍）。
    """
    key = (n, include_nsfw, fold_sequels, experience)
    if key not in _state["questions"]:
        with _conn() as c:
            items = questionnaire.select_items(
                c, n=n, include_nsfw=include_nsfw,
                fold_sequels=fold_sequels, experience=experience,
            )
        _state["questions"][key] = [
            schemas.QuestionItem(
                subject_id=it.subject_id, name=it.name, year=it.year,
                done=it.done, form=it.form, replaced_from=it.replaced_from,
            )
            for it in items
        ]
    items = _state["questions"][key]
    return schemas.QuestionnaireResponse(
        items=items, experience=experience, total=len(items)
    )


def _to_ratings(answers: list[schemas.Answer]) -> list[recommend.Rating]:
    """作答 → Rating。分数/置信度的映射全部委托给 to_rating()。"""
    out: list[recommend.Rating] = []
    for a in answers:
        try:
            t = questionnaire.to_rating(a.choice, a.score)
        except ValueError as e:                     # choice='seen' 却没带分数
            raise HTTPException(422, f"subject_id={a.subject_id}: {e}") from e
        if t is None:                               # skip：用缺失表示，不占位
            continue
        out.append(recommend.Rating(a.subject_id, t[0], t[1], t[2]))
    return out


def _reasons(cat: recommend.Catalog, pref: np.ndarray, i: int, k: int = 4) -> list[str]:
    """推荐理由：对余弦贡献最大的几个 tag。

    ⚠️ 取 pref[j] × item[j] 而不是「该作品票数最高的 tag」——
       后者与用户无关，解释不了「为什么推给你」。只取正贡献：
       负贡献是「这部有你不喜欢的元素」，不该出现在推荐理由里。
    """
    contrib = pref * cat.mat[i]
    top = np.argsort(-contrib)[:k]
    return [cat.vocab[j] for j in top if contrib[j] > 0]


@app.post("/recommend", response_model=schemas.RecommendResponse)
def post_recommend(req: schemas.RecommendRequest) -> schemas.RecommendResponse:
    """无状态打分。评分随请求传入，服务端零写入。"""
    cat = _catalog()
    ratings = _to_ratings(req.answers)
    if not ratings:
        # 空作答不是错误 —— 用户可能刚打开问卷。返回空列表让前端提示继续答题。
        return schemas.RecommendResponse(items=[], used_ratings=0,
                                         rank_by=req.rank_by)

    recs = recommend.score(
        cat, ratings, mode=req.mode, year_min=req.year_min,
        year_max=req.year_max, include_nsfw=req.include_nsfw,
        min_score=req.min_score, fold_series=req.fold_series, rank_by=req.rank_by,
        blend_alpha=req.blend_alpha, top_k=req.top_k,
    )

    # 推荐理由要用偏好向量，这里重算一次（11k×308 约 3 ms，比改 score() 签名划算）
    pref = recommend.preference_vector(cat, ratings)
    n = float(np.linalg.norm(pref))
    pref = pref / n if n else pref

    items = []
    for r in recs:
        i = cat.index_of(r.subject_id)
        items.append(schemas.Recommendation(
            subject_id=r.subject_id, name=r.name,
            year=int(cat.year[i]) or None,
            match=r.match, quality=r.quality, rank_score=r.rank_score,
            bgm_score=float(cat.bgm_score[i]) or None,
            done=int(cat.done[i]),
            reasons=_reasons(cat, pref, i),
        ))
    return schemas.RecommendResponse(items=items, used_ratings=len(ratings),
                                     rank_by=req.rank_by)


_SEARCH_COLS = """
    p.subject_id, p.name, p.name_cn, p.air_year, p.form, p.fav_done, p.score
"""


@app.get("/search", response_model=list[schemas.SearchHit])
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


@app.get("/anime/{subject_id}", response_model=schemas.AnimeDetail)
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

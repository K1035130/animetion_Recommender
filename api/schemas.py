"""请求与响应模型。

⚠️ **前端传的是「作答选项」，不是算好的分数。**
   `{choice: "wish"}` 而不是 `{score: 8.0, confidence: 0.5}`。
   分数/置信度的映射只在 `questionnaire.to_rating()` 一处维护 ——
   让前端算等于把它复制进 TypeScript，两边一漂移就是静默的推荐质量下降
   （不报错，只是权重悄悄错了）。这条与 recommend.Rating 的注释是同一条纪律。
"""

from typing import Literal

from pydantic import BaseModel, Field

from src import recommend

Choice = Literal["seen", "wish", "pass", "skip"]
Mode = Literal["all", "season", "aired", "upcoming", "recent", "classic"]
RankBy = Literal["match", "quality", "blend"]
Experience = Literal["new", "mid", "veteran"]


class Answer(BaseModel):
    """一条作答。

    来源与本模型无关 —— 游客的 localStorage、注册用户的 user_rating 表，
    甚至用户搜出来主动打的分，统统走这一个形状（第 2 节架构铁律）。
    手动打分表示成 choice="seen" + score。
    """

    subject_id: int
    choice: Choice
    # choice="seen" 时必填，其余选项忽略。
    score: float | None = Field(default=None, ge=1, le=10)


class QuestionItem(BaseModel):
    subject_id: int
    name: str
    year: int | None
    done: int                      # 收藏「看过」人数，前端可用来排版/提示热度
    form: str | None
    # 若该题由某部续作折叠而来，记下原始 id。前端不用管，排查问题时有用。
    replaced_from: int | None = None


class QuestionnaireResponse(BaseModel):
    items: list[QuestionItem]
    # 回显口径，方便前端展示「正在问最近 N 年的番」
    experience: Experience
    total: int


class Recommendation(BaseModel):
    """一条推荐。

    ⚠️ 列表恒按 `rank_score` 降序，**不要按 match 或 quality 重排**。
       match 在 blend/quality 模式下会大小交错，那是预期的。
    """

    subject_id: int
    name: str
    year: int | None
    match: float                   # 与偏好向量的余弦，[-1,1]
    quality: float                 # 贝叶斯加权评分，[0,10]
    rank_score: float              # 排序依据，量纲随 rank_by 变
    bgm_score: float | None        # Bangumi 原始均分，展示用
    done: int
    # 推荐理由：对匹配度贡献最大的几个 tag（pref[j] × item[j] 降序）。
    # ⚠️ 不是「该作品票数最高的 tag」—— 那与用户无关，解释不了「为什么推给你」。
    reasons: list[str]


class RecommendRequest(BaseModel):
    answers: list[Answer]
    mode: Mode = "all"
    # 显式年份区间优先于 mode 预设，两者都给时不取交集（交集易出空结果且难排查）
    year_min: int | None = None
    year_max: int | None = None
    include_nsfw: bool = False
    # 硬性评分下限，传 null 关闭。默认 3.5 —— 低分是比高分可靠得多的信号。
    # ⚠️ 只排除「有评分且低于下限」的；未评分作品放行（新番还没人打分）。
    min_score: float | None = Field(default=recommend.MIN_SCORE, ge=0, le=10)
    fold_series: bool = True
    rank_by: RankBy = "blend"
    blend_alpha: float = Field(default=0.5, ge=0.0, le=1.0)
    top_k: int = Field(default=20, ge=1, le=100)


class RecommendResponse(BaseModel):
    items: list[Recommendation]
    # 有效信号数（skip 不计）。前端可据此提示「再答几题会更准」。
    used_ratings: int
    rank_by: RankBy


class SearchHit(BaseModel):
    subject_id: int
    name: str
    name_cn: str | None
    year: int | None
    form: str | None
    done: int
    bgm_score: float | None
    # 命中方式：tsv = BM25 正常命中，trgm = 名字拼错时的模糊兜底
    via: Literal["tsv", "trgm"]


class AnimeDetail(BaseModel):
    subject_id: int
    name: str
    name_cn: str | None
    name_en: str | None
    summary: str | None
    air_date: str | None
    air_year: int | None
    form: str | None
    nsfw: bool
    bgm_score: float | None
    score_count: int | None
    rank: int | None
    done: int
    tags: list[str]                # 清洗后落在词表内的题材 tag
    meta_tags: list[str]
    studios: list[str]
    anilist_id: int | None

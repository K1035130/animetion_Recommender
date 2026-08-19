"""请求与响应模型。

⚠️ **前端传的是「作答选项」，不是算好的分数。**
   `{choice: "wish"}` 而不是 `{score: 8.0, confidence: 0.5}`。
   分数/置信度的映射只在 `questionnaire.to_rating()` 一处维护 ——
   让前端算等于把它复制进 TypeScript，两边一漂移就是静默的推荐质量下降
   （不报错，只是权重悄悄错了）。这条与 recommend.Rating 的注释是同一条纪律。
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

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
    # P1 三路相似度的融合权重。不传则用 src/recommend.py 的 DEFAULT_WEIGHTS。
    #
    # ⚠️ **暴露出来是为了第 5 周的评测能扫参数与跑 baseline**，不是给前端用的。
    #    第 10 节的四条 baseline 直接由它构造：
    #      tag 模型       w_tag=1, w_emb=0, w_staff=0
    #      embedding 模型 w_tag=0, w_emb=1, w_staff=0
    #
    # ⚠️ 权重不必和为 1 —— 服务端按实际参与的路数归一化。
    #    某一路的偏好向量为零（如用户评过的作品全无 staff 数据）时整项跳过，
    #    而不是贡献 0 —— 后者会一视同仁地稀释另外两路。
    #
    # ⚠️ 三个必须一起给或一起不给。只给一个的话另外两个会取默认值，
    #    组合出一个谁也没想要的权重 —— 所以下面的校验会拒绝部分指定。
    w_tag: float | None = Field(default=None, ge=0.0, le=1.0)
    w_emb: float | None = Field(default=None, ge=0.0, le=1.0)
    w_staff: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def _weights_all_or_none(self) -> "RecommendRequest":
        given = [w for w in (self.w_tag, self.w_emb, self.w_staff) if w is not None]
        if given and len(given) != 3:
            raise ValueError("w_tag / w_emb / w_staff 必须三个一起给")
        if given and sum(given) <= 0:
            raise ValueError("三个融合权重不能全为 0")
        return self


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


# ── 流程 C · 剧情问答（阶段 05）────────────────────────────────────
# ⚠️ 与推荐链路不同，这条路上**有模型调用**（embedding + rerank + LLM），
#    所以响应里带 meta 做溯源：哪个 reranker、哪个 LLM、召回了多少条。
#    第 5 周评测要靠它区分「检索没召回」和「模型没答对」——
#    G.5f 那次判分错误正是因为分不清这两者。

AskState = Literal["ok", "ambiguous", "no_corpus", "unknown"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)

    # ⚠️ 默认关。剧透门控是主防线（第 15 节原则 2），放开必须由用户**显式**确认。
    #    但 spoiler=False 不等于结果里没有剧透 —— `spoiler_level` 那一列的
    #    召回率已知不完整（F.4 ③：2,529 条里 heimu 占 2,421、剧透框仅 150），
    #    所以前端要**无条件**带一句「回答可能包含剧透」，不要依赖这个开关。
    spoiler: bool = False

    # 交给 LLM 的 chunk 条数。G.6 实测定案是 8。
    top_k: int = Field(default=8, ge=1, le=20)


class AskChunk(BaseModel):
    """一条被采纳的语料，前端用来展示出处。"""

    chunk_id: int
    section: str | None            # 角色名或章节名，即 prompt 里【】中的内容
    text: str
    kind: Literal["prose", "songs", "profile"]
    source: Literal["moegirl", "bangumi_char"]
    spoiler_level: int
    # rerank 相关度。⚠️ 量纲不跨查询可比，只用于同一次结果内排序，别展示成百分比。
    score: float | None
    # 是否由 ① alias 直取而来（而非向量召回）。见 retrieve.PIN_RESERVE。
    pinned: bool


class AskCandidate(BaseModel):
    """state=ambiguous 时的候选作品，前端渲染成可点的选项。"""

    series_root: int
    title: str


class AskResponse(BaseModel):
    state: AskState
    # state=ok 时是回答正文；其余三种状态是给用户看的说明（反问 / 没语料 / 没认出）。
    answer: str | None
    series_root: int | None
    title: str | None
    chunks: list[AskChunk]
    candidates: list[AskCandidate]
    meta: dict

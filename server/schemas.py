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
    # 卡片简介，已截断（questionnaire.SUMMARY_MAX_CHARS）。可能为 None——
    # 589 部作品没有 summary（A.9），前端要按缺失处理，不要留白当出错。
    summary: str | None = None


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


# 单一输入框背后的分岔口。"auto" = 服务端自己判（src/router.py）。
# ⚠️ **按钮不是独立功能，而是强制指定 route 的参数** —— 第四部分那条
#    「单一入口 vs 功能按钮」的结论：两者不是二选一，单一入口涵盖按钮。
# 📌 route 同时是**评测入口**：四条分支混在一个端点里，「推荐的 NDCG」和
#    「问答的准确率」会被搅在一起，评测必须能直接打各条子路径。
AskRoute = Literal["auto", "ask", "voice", "season", "find"]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)

    # ⚠️ 指定了就**强制**走那条分支，但空结果时会回落到 ask 并在
    #    route_reason 里说明 —— 硬失败会让用户以为功能坏了。
    route: AskRoute = "auto"

    # ⚠️ 默认关。剧透门控是主防线（第 15 节原则 2），放开必须由用户**显式**确认。
    #    但 spoiler=False 不等于结果里没有剧透 —— `spoiler_level` 那一列的
    #    召回率已知不完整（F.4 ③：2,529 条里 heimu 占 2,421、剧透框仅 150），
    #    所以前端要**无条件**带一句「回答可能包含剧透」，不要依赖这个开关。
    spoiler: bool = False

    # 交给 LLM 的 chunk 条数。G.6 实测定案是 8。
    top_k: int = Field(default=8, ge=1, le=20)

    # ── 多轮对话（2026-08-19 加）───────────────────────────────────
    # ⚠️ **服务端不存会话状态，历史由调用方传入** —— 与第 2 节那条
    #    「评分随请求传入」的架构铁律同源：游客的 localStorage 与将来
    #    注册用户的会话表走同一个入口，这一层不区分是谁传来的。

    # 上一轮反问「你是指哪一部？」时，用户点中的那个候选的 series_root。
    # ⚠️ **给了就无条件钉死作用域**，不再解析作品名 —— 这是用户的显式指令。
    #    消歧回合本可以让 LLM 去猜「我要第二个」，但那是白白引入不确定性
    #    （第 15 节原则 2：能用规则判的别交给模型）。
    scope: int | None = None

    # 最近几轮 (问, 答)，用来消解「她」「那结局呢」这类指代。
    # ⚠️ 只在**当前问句自己认不出实体时**才用来继承作用域，否则
    #    「聊完进击的巨人接着问芙莉莲」会被静默锁死在上一部作品里。
    # 🚨 服务端还会再截一次（retrieve.MAX_HISTORY_TURNS / MAX_HISTORY_CHARS）：
    #    上下文噪声会把 LLM 逼成拒答，这是 I.2 ② 实测过的。
    history: list[tuple[str, str]] = Field(default_factory=list, max_length=10)


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
    """state=ambiguous 时的候选作品，前端渲染成可点的选项。

    ⚠️ **前端要把 `series_root` 原样回传到下一轮的 `scope`**，
       否则那句「你是指哪一部？」就是死路 —— 用户选了也没地方送。
    """

    series_root: int
    title: str
    # ⚠️ 同名重制版必须靠年份区分：《多罗罗》1969 与 2019 是两个系列根，
    #    只显示标题的话两个选项长得一模一样，用户无从选择（全库 81 例）。
    year: int | None = None


class AskResponse(BaseModel):
    # ⚠️ **实际走的分支，必须回传。** 路由错了是**静默**的 —— 用户会以为
    #    系统笨，而不知道是分错了路。前端要把它显示出来并允许改：
    #    「我理解为：查询 2016 年夏季新番 ▾」比静默猜测好得多。
    route: str
    route_reason: str

    state: AskState
    # state=ok 时是回答正文；其余三种状态是给用户看的说明（反问 / 没语料 / 没认出）。
    answer: str | None
    series_root: int | None
    title: str | None
    chunks: list[AskChunk]
    candidates: list[AskCandidate]
    meta: dict

    # 非 ask 分支的结构化结果，按 route 二选一（都为 None 就是走了 ask）。
    # ⚠️ 这两条分支**零模型调用**，答案直接来自库 —— 与 /ask 的 3~6 秒
    #    不是一个量级，前端可以不给 loading 态。
    # ⚠️ 字符串注解 + 文件末尾 model_rebuild()：这两个模型定义在本类**之后**
    #    （文件按「端点分组」排版，不按依赖序）。改动它们的位置时别忘了 rebuild。
    voice: "VoiceResponse | None" = None
    season: "SeasonResponse | None" = None
    find: "FindResponse | None" = None


# ── 结构化关联查询（src/related.py）────────────────────────────────
# ⚠️ 与 /ask 的性质完全不同：这里**零模型调用**，答案直接来自 staff / studios
#    两列，精确且零幻觉。「某某还做过什么」是关联查询不是语义检索。

RelatedRole = Literal["原作", "导演", "脚本", "人物设定", "音乐", "制作公司"]


class RelatedWork(BaseModel):
    series_root: int
    subject_id: int
    name: str
    name_cn: str | None
    air_year: int | None
    fav_done: int
    # 因为哪个 facet 关联上的。⚠️ via_name 是**日文原形**（staff 列的存法），
    #    与萌娘正文里的简体写法不同 —— 详见 src/related.py 的模块注释。
    via_role: RelatedRole
    via_name: str


class RelatedResponse(BaseModel):
    series_root: int
    title: str | None
    items: list[RelatedWork]


# ── 声优配役（GET /api/voice）────────────────────────────────────
# ⚠️ 与 /related 同一类：结构化事实查询，**零模型调用**。
#    role_type 原样返回给前端（1=主角 2=配角 3=客串，可能为 null），
#    由展示层决定怎么标 —— 服务端不做成中文字符串，否则前端要反解析。


class VoiceRoleItem(BaseModel):
    character_id: int
    # ⚠️ 可能为 null：voice_role 涉及 82,814 个角色，其中 30.9% 在 alias 里
    #    查不到名字（那些角色没写简介，没被 build_char_chunks 收进来）。
    #    如实返回 null，不要填「未知」之类的占位串。
    character_name: str | None
    series_root: int
    title: str
    air_year: int | None
    role_type: int | None
    fav_done: int | None


class VoiceResponse(BaseModel):
    person_id: int
    name: str                 # dump 原名，多为日文
    name_cn: str | None
    n_roles: int              # 库内配役总数（未截断），items 是其中前 limit 条
    items: list[VoiceRoleItem]


# ── 按档期浏览（GET /api/season）──────────────────────────────────
# ⚠️ 无个性化的浏览，**不挂在 /recommend 上** —— 那个要传评分才能算偏好向量，
#    而「这个季度在播什么」谁来问答案都一样。零模型调用。


class SeasonItem(BaseModel):
    subject_id: int
    name: str
    name_cn: str | None
    air_date: str
    form: str | None
    done: int
    bgm_score: float | None


class SeasonResponse(BaseModel):
    # 归一化后的季度（month 恒为 1/4/7/10），查询传 8 月会归到 7 月番
    year: int
    month: int
    # 窗口口径：recommend.cour_window —— 本季起点 −7 天 ~ 下季起点（右开）
    window_start: str
    window_end: str
    # 窗口内的总数（items 受 limit 截断，total 不受）
    total: int
    items: list[SeasonItem]


# ── 找番（GET /api/find，流程 B · G.1 路径①）──────────────────────
# ⚠️ 与 /related、/voice、/season 同一类：不是"推荐"（无个性化偏好向量），
#    是"用一段描述做语义检索"。零折叠地址复用 recommend_sql 的续作折叠 +
#    nsfw 过滤（见 src/find.py 模块注释）。**唯一会调模型的零模型端点例外**——
#    只调 embedding 一次，不调 LLM/rerank，实测延迟量级与 /search 相近。


class FindHit(BaseModel):
    """与 `Recommendation`（第 53 行）同一套约定——单一 `name`
    （已 COALESCE(name_cn, name)），不单独暴露 name_cn。"""

    subject_id: int
    name: str
    air_year: int | None
    # 语义腿余弦，[-1,1]。⚠️ 量纲不跨请求可比，只用于同一次结果内部排序。
    match: float


class FindResponse(BaseModel):
    query: str
    items: list[FindHit]


# ── 账号系统（第 6 周）────────────────────────────────────────────
# ⚠️ **评分同步复用上面的 `Answer`**，不另定义一套。那是 /recommend 的请求
#    形状，也是 localStorage 里存的形状 —— 三处同形正是「游客与注册用户走
#    同一条代码路径」这条铁律的体现。另开一套等于给自己埋一次转换 bug。


class RegisterRequest(BaseModel):
    email: str = Field(max_length=254)
    # 上下限与 src/auth.py 的 PASSWORD_MIN/MAX 一致。
    # ⚠️ 上限不是安全要求，是防「有人 POST 一个 1 MB 的密码」把 argon2 拖死。
    password: str = Field(min_length=8, max_length=128)
    # 游客转正：注册时把 localStorage 里已有的评分一起带上，
    # 不让用户白答一遍问卷（设计文档「双轨会话」）。
    guest_ratings: list[Answer] = Field(default_factory=list, max_length=2000)


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)
    # 登录时同样可以带上本地评分。⚠️ 合并规则是**云端为准、本地只补空缺**
    # （ratings.merge_guest 的 DO NOTHING）—— 账号是跨设备的事实来源，
    # 让本地覆盖云端等于用旧数据洗掉新数据，且用户看不出发生了什么。
    guest_ratings: list[Answer] = Field(default_factory=list, max_length=2000)


class QuotaStatus(BaseModel):
    """问答配额（每人 24 小时 10 条）。前端据此显示「今天还能问 N 条」。"""

    used: int
    limit: int
    remaining: int
    # 仅在用满时有值：最早那条何时滚出 24 小时窗口。
    # ⚠️ 没用满时恒为 null —— 那时「最早一条何时过期」对用户毫无信息量，
    #    给了反而让人以为要等。
    reset_at: str | None = None


class AuthUser(BaseModel):
    user_id: int
    email: str
    created_at: str
    # 已同步到账号的评分条数，前端可用来提示「已保存 N 部」。
    rating_count: int
    quota: QuotaStatus


class RatingsResponse(BaseModel):
    """该用户已保存的评分。⚠️ 形状与 RecommendRequest.answers 一致，
    前端拿到直接喂给 /recommend，中间不需要任何转换。"""

    items: list[Answer]


class RatingsSyncRequest(BaseModel):
    items: list[Answer] = Field(max_length=2000)
    # 'questionnaire' = 问卷里引导打的，'manual' = 主动搜出来打的。
    # 两者置信度不同，设计文档 §4 为后续加权预留了这个字段。
    source: Literal["questionnaire", "manual"] = "manual"


class RatingsSyncResponse(BaseModel):
    written: int
    deleted: int          # choice='skip' 会删掉已有行（「没看过」用缺失表示）
    total: int


# ⚠️ AskResponse 用字符串注解引用了下面才定义的 VoiceResponse / SeasonResponse，
#    必须在两者都定义完之后重建，否则 FastAPI 生成 schema 时会炸。
AskResponse.model_rebuild()

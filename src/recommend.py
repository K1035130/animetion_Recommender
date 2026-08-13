"""P0 推荐：tag 向量余弦 + mean-centered 打分。

⚠️ **架构铁律（第 2 节）：打分接口是无状态的 —— 评分随请求传入。**
    `score()` 只接受一组 (subject_id, rating)，不关心它来自游客的
    localStorage 还是注册用户的 user_rating 表。第 6 周加账号系统时
    只是多一个数据来源，不用回头重写推荐链路，游客与注册用户也永远
    走同一条代码路径（少一半 bug，评测时也不会出现两套口径）。

维度空间 = 清洗后的题材 tag 词表（见 data/interim/tag_vocab.json）。
用户 tag 与官方 meta_tags 落进**同一个**空间：meta_tags 同样过
tag_rules 的 normalize()+classify()，形态/地区会被自动分流掉。
这对「清洗后 tags 为空」的作品是必需的 —— 化物语(done=37,573) 的 11 个
用户 tag 全是 staff/CV/年份，只有 meta_tags 的「奇幻」「小说改」能给它向量。

⚠️ 但兜底效果有限：tags 为空的 166 部里只有 24 部能靠 meta_tags 拿到非零向量，
   其余 142 部的 meta_tags 全是形态+地区（`['TV','中国']`），过 classify()
   后被分流干净。这 142 部（多为欧美动画与国产老动画）在 P0 里无法被
   tag 余弦召回，只能等第 3 周的 embedding —— 简介文本人人都有。
"""

import datetime
from dataclasses import dataclass
from typing import Literal

import numpy as np
import psycopg

from src import series, tagvec

Weighting = Literal["logtf-idf", "binary"]
Mode = Literal["all", "season", "aired", "upcoming", "recent", "classic"]

# 日本动画按季度（cour）放送，每季起始于 1/4/7/10 月 1 日。
# 实测库内 TV+WEB 作品 75.4% 集中在这四个月开播（1月16.6% 4月21.8%
# 7月16.9% 10月20.1%），其余 8 个月合计才 24.6% —— 季度结构是真实的。
COUR_MONTHS = (1, 4, 7, 10)
# 季度起点往前的宽限天数：番剧常在季度前几天抢跑，
# 例如「我的英雄学院 第二季」2017-03-25 开播，属于当年 4 月番。
#
# ⚠️ 这个值取自行业惯例而非数据拐点 —— 实测「提前 N 天开播」的分布是
#    平坦长尾，没有拐点。唯一的尖峰在「提前 1 天」（70 部），但那是
#    12/31、3/31 的**年末/季末特番**（猫物语（黑）、卫宫家今天的饭、
#    齐木楠雄完结篇），不是抢跑的季番，放宽到它们并无意义。
#    7 天覆盖季度前月尾部的 50%，再放宽会开始混入三月中旬的独立作品。
COUR_GRACE_DAYS = 7
RankBy = Literal["match", "quality", "blend"]

# blend 模式里匹配度占的权重，1-α 给评分。0.5 = 等权。
# ⚠️ 两个量纲差得远（余弦实测 0.2–0.45，加权评分 4–9），必须先在
#    候选池内各自 min-max 归一化再加权，直接相加等于只看评分。
DEFAULT_ALPHA = 0.5

CLASSIC_BEFORE = 2011      # 第 7 节「经典回顾」的分界线
# 「当季新番」的时间窗。第 7 节写的是「最近 1–2 季」，一季 3 个月。
# 往回 6 个月覆盖两季（容纳「上季末播出、本季仍在追」）；
# 往前 3 个月覆盖下季已定档的作品 —— 用户挑「这季看什么」时，
# 刚公布档期的新番同样有参考价值。
# ⚠️ 上界不能省：库里有 2027 年的条目，只设下界的话「最近半年」
#    会把一年半后的未播作品也算进来。
RECENT_BACK_MONTHS = 6
RECENT_AHEAD_MONTHS = 3

# mean-centering 的先验。取值不是拍脑袋 —— 由库内 score_details 直方图
# 逐票统计得出：全站 3,900 万张选票的均分是 7.074，即「典型用户给典型作品
# 打多少分」。按作品平均是 6.30，但那会被大量冷门低分作品拉低，不是用户视角。
PRIOR_MEAN = 7.07
# 先验相当于多少条虚拟评分。2 条时：单条 10 分 → μ=8.05，权重 +1.95（能出推荐）；
# 单条 3 分 → μ=5.71，权重 -2.71（推荐不相似的，也合理）。
# 评分攒到 10 条以上时先验的影响已可忽略，用户自己的均值重新主导。
PRIOR_WEIGHT = 2.0

# 贝叶斯加权评分里的「先验票数」。取库内评分人数的中位数（286）圆整。
# 票数低于它的作品，其评分会被显著拉向全站均分。
VOTE_PRIOR = 300

# 推荐结果的评分下限（2026-08-12 加）。低于它的作品直接不进候选。
#
# 依据是评分信号的**不对称性**：高分不保证好看（小众神作与过誉作品混在一起），
# 但低分几乎必然难看 —— 实测 78 部低于 3.5 的作品里，最热门的是
# 三体(1.70)、兽娘动物园2(1.40)、约定的梦幻岛第二季(3.30)、国王游戏(2.70)，
# 全是公认的翻车作。这类作品**恰恰容易被 tag 余弦召回**：它们的题材标签
# 与原作/前作一致，向量上和用户口味高度吻合，烂的是执行不是题材。
#
# ⚠️ 用**原始均分**而不是 wr。wr 会向 7.07 收缩，60 票打 3.0 的作品
#    wr 高达 6.39，按 wr 卡这条线等于什么都没过滤。
# ⚠️ 只有 0.68%（78/11453）会被排除，对候选池规模无影响。
#    其中 26 部票数不足 100（最少 25 票），单看统计确实噪声偏大，
#    但方向是对的：误伤几部冷门作品的代价远小于推出一部烂片。
MIN_SCORE = 3.5
# 两段式排序里，第一段按匹配度召回多少候选。太小会让质量排序无从选起，
# 太大会把弱相关作品放进来 —— 取 top_k 的 10 倍，下限 100。
DEFAULT_POOL_FACTOR = 10
DEFAULT_POOL_MIN = 100


@dataclass(frozen=True)
class Rating:
    """一条评分。confidence 让「看过并打分」与「没看过但想尝试」不等权。

    三种作答到 (分数, 置信度) 的映射由 questionnaire.to_rating() 定义 ——
    具体数值只在那一处维护，不要在这里复述，两边漂移了很难发现。
    """

    subject_id: int
    score: float
    confidence: float = 1.0
    # 是否把这部从推荐结果里剔除。**不等于「是否作答过」**：
    #   看过并打分 → True（已经看过了）
    #   没看过但想尝试 → **False** —— 用户明确说了想看，正是该推的
    #   没看过也不感兴趣 → True（明确说了不想看）
    # 默认 True，因为裸元组 (sid, score) 表示的是真实评分。
    exclude: bool = True

    @staticmethod
    def coerce(x: "Rating | tuple") -> "Rating":
        if isinstance(x, Rating):
            return x
        return Rating(*x)          # 兼容 (sid, score) / (sid, score, conf)


@dataclass
class Catalog:
    """全库的 tag 矩阵。一次构建，多次打分。"""

    ids: np.ndarray            # (n,) subject_id
    vocab: list[str]           # (d,) 维度名，供解释推荐理由用
    mat: np.ndarray            # (n, d) float32，**行已 L2 归一化**
    year: np.ndarray           # (n,) air_year
    # (n,) 年*100+月。「当季新番」是本项目的核心场景（每季 50–80 部里挑），
    # 只有年粒度做不了「最近 1–2 季」。air_date 为空时退化成 年*100+0，
    # 这样按 ym 比较仍然落在正确的年份区间内。
    ym: np.ndarray
    # (n,) air_date 的序数（date.toordinal()），无日期则为 0。
    # ym 只到月粒度，做不了「今天是否已开播」这种日级比较。
    air_ord: np.ndarray
    nsfw: np.ndarray           # (n,) bool
    done: np.ndarray           # (n,) fav_done，热度兜底与多样性用
    name: list[str]            # (n,) 展示名
    bgm_score: np.ndarray      # (n,) Bangumi 均分，无评分则为 0
    votes: np.ndarray          # (n,) 评分人数
    # (n,) 贝叶斯加权后的评分。直接用 bgm_score 排序会被冷门作品的
    # 虚高分污染 —— 实测「甲壳虫乐队」5 人打 9.2、「音乐之声」19 人打 9.2，
    # 会排在 CLANNAD AFTER STORY（3.1 万人 9.2）前面。
    #     wr = v/(v+m)·R + m/(v+m)·C
    # C 取全站逐票均分 7.07，m 取评分人数中位数 286 → 圆整到 300。
    wr: np.ndarray

    def index_of(self, subject_id: int) -> int | None:
        hit = np.flatnonzero(self.ids == subject_id)
        return int(hit[0]) if len(hit) else None


@dataclass(frozen=True)
class Recommendation:
    """一条推荐。三个分数各有各的用途，**不要互相换算**。

    ⚠️ 曾经的接口返回 (sid, name, match) 三元组，而列表顺序由 rank_by 决定、
       并不按 match 降序 —— 消费方按第三项重排就会悄悄改变结果。
       现在把排序依据显式化成 `rank_score`，**列表恒按它降序**，
       这条不变式对三种 rank_by 都成立。
    """

    subject_id: int
    name: str
    # 偏好向量与作品向量的余弦，[-1,1]。正 = 比此人平均口味更对味。
    match: float
    # 贝叶斯加权评分，[0,10]。与用户无关，是作品自身的口碑。
    quality: float
    # 实际排序依据，列表恒按它降序：
    #   rank_by="match"   → == match（[-1,1]）
    #   rank_by="quality" → == quality（[0,10]）
    #   rank_by="blend"   → 池内归一化后的加权和（[0,1]）
    # ⚠️ 量纲随 rank_by 变，**不要**拿它跨请求比较或展示给用户。
    rank_score: float


def build_catalog(conn: psycopg.Connection,
                  weighting: Weighting = "logtf-idf") -> Catalog:
    """从库里拉出全部作品与预计算好的 tag 向量，装配成内存矩阵。

    ⚠️ **向量不在这里算，直接读 anime_profile.tag_vec。**（2026-08-12 改）
       此前是每次启动用 log1p×idf×L2 重算一遍（1.31 s）。改成读库之后：
         · 线上 SQL 打分（recommend_sql）与这里读的是**同一批数字**，
           两条路径口径一致成了构造上的保证，不靠人记得同步
         · 顺带省掉每次启动的 Python 侧 tag 清洗
       计算逻辑搬到 src/tagvec.py，由 scripts/build_tag_vectors.py 写入。
       ⚠️ 改了词表 / tag_rules / tagvec 之后**必须重跑那个脚本**，
          否则这里读到的是旧向量。

    ⚠️ binary 模式（第 5 周 ablation 用）仍然现算 —— 库里只存 logtf-idf 一种。
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, COALESCE(name_cn, name), air_year, nsfw, fav_done,
                   COALESCE(EXTRACT(MONTH FROM air_date), 0)::int,
                   COALESCE(score, 0)::float, COALESCE(score_count, 0)::int,
                   air_date, tag_vec, tags, meta_tags
            FROM anime_profile
            ORDER BY subject_id
        """)
        rows = cur.fetchall()

    vlist = tagvec.vocab()
    ids, years, nsfws, dones, names, yms = [], [], [], [], [], []
    scores, votes, ords = [], [], []
    mat = np.zeros((len(rows), len(vlist)), dtype=np.float32)
    missing = 0
    for i, (sid, nm, yr, nsfw, done, mon, sc, vt, adate, vec, _t, _m) in enumerate(rows):
        if vec is not None:
            v = vec.to_numpy() if hasattr(vec, "to_numpy") else np.asarray(vec)
            mat[i] = v.astype(np.float32)
        else:
            missing += 1          # 零向量作品，tag_vec 存 NULL，保持整行为零
        ids.append(sid)
        years.append(yr)
        yms.append((yr or 0) * 100 + (mon or 0))
        nsfws.append(nsfw)
        dones.append(done)
        names.append(nm)
        scores.append(sc)
        votes.append(vt)
        ords.append(adate.toordinal() if adate else 0)

    if weighting == "binary":
        # ablation 分支：binary 不入库，现场重算
        _, mat = tagvec.compute([(r[0], r[10], r[11]) for r in rows],
                                weighting="binary")
    elif missing == len(rows):
        raise RuntimeError(
            "anime_profile.tag_vec 整列为空 —— 先跑 "
            "scripts/build_tag_vectors.py。（否则打分会静默返回空结果）")

    return Catalog(ids=np.array(ids), vocab=vlist, mat=mat,
                   year=np.array([y if y is not None else 0 for y in years]),
                   ym=np.array(yms), air_ord=np.array(ords),
                   nsfw=np.array(nsfws, dtype=bool),
                   done=np.array(dones), name=names,
                   bgm_score=np.array(scores, dtype=np.float32),
                   votes=np.array(votes), wr=_weighted_rating(
                       np.array(scores, dtype=np.float32),
                       np.array(votes, dtype=np.float32)))


def _weighted_rating(score: np.ndarray, votes: np.ndarray) -> np.ndarray:
    """贝叶斯加权评分：票数少的作品向全站均分收缩。

    没有评分的作品（score=0）直接取先验，否则会被当成「0 分」排到最后。
    """
    wr = ((votes / (votes + VOTE_PRIOR)) * score
          + (VOTE_PRIOR / (votes + VOTE_PRIOR)) * PRIOR_MEAN)
    return np.where(score > 0, wr, PRIOR_MEAN).astype(np.float32)


def _cour_start(year: int, month: int) -> datetime.date:
    """该月所属季度的起始日（1/4/7/10 月 1 日）。"""
    return datetime.date(year, ((month - 1) // 3) * 3 + 1, 1)


def _shift_cour(d: datetime.date, n: int) -> datetime.date:
    """把季度起点前后移 n 个季度。"""
    idx = d.year * 4 + (d.month - 1) // 3 + n
    return datetime.date(idx // 4, (idx % 4) * 3 + 1, 1)


def season_window(today: datetime.date | None = None
                  ) -> tuple[datetime.date, datetime.date]:
    """「当季」窗口 = 前一季度起点（含宽限） ~ 后一季度终点。

    以 9 月为例：当前季度是 7 月番，前一季 4 月、后一季 10 月，
    窗口 = 3/25 ~ 12/31，正好覆盖 4/7/10 三个季度。
    「已开播」「未开播」在这个窗口内再按今天切一刀。
    """
    today = today or datetime.datetime.now(datetime.UTC).date()
    cur = _cour_start(today.year, today.month)
    lo = _shift_cour(cur, -1) - datetime.timedelta(days=COUR_GRACE_DAYS)
    hi = _shift_cour(cur, 2) - datetime.timedelta(days=1)
    return lo, hi


def _months_ago(n: int) -> int:
    """n 个月前的「年*100+月」。

    ⚠️ 必须用 divmod 做月份借位。写成 `(m//12)*100 + (m%12 or 12)` 会在
       m 能被 12 整除时（每年 6 月）算出 202612 而不是 202512，差整整一年。
    """
    # 用 UTC 而非本地时区：窗口是半年，差一天无所谓，但显式写出来
    # 免得在不同部署环境（本地 CST / Render 的 UTC）算出不同结果。
    today = datetime.datetime.now(datetime.UTC).date()
    y, mo = divmod(today.year * 12 + (today.month - 1) - n, 12)
    return y * 100 + mo + 1


def preference_vector(cat: Catalog, ratings: "list[Rating | tuple]",
                      *, prior_mean: float = PRIOR_MEAN,
                      prior_weight: float = PRIOR_WEIGHT) -> np.ndarray:
    """把一组评分压成一个 mean-centered 的偏好向量。

    减去用户自己的均分是关键：有人习惯全打 8–10，有人全打 4–6。
    不减的话前者的每一部都会被当成「喜欢」，学到的其实是打分习惯而非口味。
    减完之后正权重 = 高于此人平均水平，负权重 = 低于 —— 后者同样有信息，
    应当把相似的作品往下压。

    ⚠️ **但纯用户均值在两种情况下会整个退化成零向量**，实测发现：
      · 只评了一部 —— μ 就等于那一部的分，权重恒为 0
      · 所有评分相同（用户全打 8 分）—— 同理
    问卷流程里这是致命的：答完第一题就该有反馈，却会拿到空结果。

    所以 μ 用**向先验收缩**的形式：
        μ = (Σr + k·prior) / (n + k)
    n 小时 μ 靠近 prior（单条 10 分 → 权重为正，能出推荐）；
    n 大时 μ 收敛到用户自己的均值（保住去除打分习惯的本意）。
    这是标准的贝叶斯收缩，不是为了绕过边界情况打的补丁。
    """
    rs = [Rating.coerce(x) for x in ratings]
    rs = [r for r in rs if cat.index_of(r.subject_id) is not None]
    if not rs:
        return np.zeros(cat.mat.shape[1], dtype=np.float32)

    # 均值也按置信度加权 —— 否则一堆低置信的「不感兴趣」会把 μ 整个拉低，
    # 使得所有真实评分都变成正权重，等于没做 centering。
    cw = sum(r.confidence for r in rs)
    mu = ((sum(r.confidence * r.score for r in rs) + prior_weight * prior_mean)
          / (cw + prior_weight))

    rows = [cat.index_of(r.subject_id) for r in rs]
    w = np.array([r.confidence * (r.score - mu) for r in rs],
                 dtype=np.float32)[:, None]
    return (cat.mat[rows] * w).sum(axis=0)


def score(cat: Catalog,
          ratings: "list[Rating | tuple]",
          *,
          mode: Mode = "all",
          year_min: int | None = None,
          year_max: int | None = None,
          include_nsfw: bool = False,
          min_score: float | None = MIN_SCORE,
          fold_series: bool = True,
          rank_by: RankBy = "blend",
          blend_alpha: float = DEFAULT_ALPHA,
          top_k: int = 20) -> list[Recommendation]:
    """无状态打分。返回按 `rank_score` 降序的 Recommendation 列表。

    `min_score` 是硬性质量下限（默认 MIN_SCORE=3.5），传 None 关闭。
    ⚠️ 第 5 周跑 baseline 时**四条线必须用同一个 min_score**，否则候选池
       口径不一致，NDCG 没有可比性。要么全开、要么全传 None。

    ⚠️ **匹配度不是排序依据。** `rank_by` 决定 `rank_score` 怎么算
       （见 Recommendation 的字段注释），列表恒按 `rank_score` 降序，
       而 `match` 在 quality/blend 模式下会出现「大小交错」—— 那是预期的。
       消费方**直接按返回顺序展示**即可，不需要也不应该重排。
    """
    pref = preference_vector(cat, ratings)
    n = np.linalg.norm(pref)
    if n == 0:
        return []
    sims = cat.mat @ (pref / n)

    # ⚠️ 零向量作品必须排除。它们与任何偏好向量的余弦都是 0，而偏好向量
    #    整体为负时（用户对问卷里多数作品选了「不感兴趣」），0 反而**高于**
    #    所有负相关作品，于是信息量为零的作品会排到最前面 —— 实测「全部
    #    不感兴趣」时 Top5 全是虫虫危机、隐形墨水这类没有 tag 的条目。
    #    tag 模型本就无法为它们打分，放进候选是不诚实的。
    #    这 142 部（多为欧美动画与国产老动画）要等第 3 周的 embedding。
    keep = cat.mat.any(axis=1)
    if not include_nsfw:
        keep &= ~cat.nsfw                          # 第 13 节：入库保留、默认过滤
    if min_score is not None:
        # ⚠️ 判据是「**有评分**且低于下限」，没评分的（bgm_score<=0）放行 ——
        #    「还没人打分」不等于「难看」。写成 `>= min_score` 会把未评分作品
        #    一并排除，而 mode="upcoming" 推的正是还没播的新番：
        #    当前 dump 里那 2 部碰巧有开播前评分，所以现在看不出问题，
        #    但第 6 周季度同步接进真正的新公布作品后，upcoming 档会被
        #    **静默清空**（不报错，只是永远返回空列表）。
        keep &= (cat.bgm_score >= min_score) | (cat.bgm_score <= 0)
    # 年份区间。显式的 year_min/year_max 优先于 mode 预设 ——
    # 两者都给时以显式区间为准，不做交集（交集容易出空结果且难排查）。
    if year_min is not None or year_max is not None:
        if year_min is not None:
            keep &= cat.year >= year_min
        if year_max is not None:
            keep &= cat.year <= year_max
    elif mode == "classic":
        keep &= cat.year < CLASSIC_BEFORE
    elif mode in ("season", "aired", "upcoming", "recent"):
        # ⚠️ 曾把 recent 实现成「>= 2011」，那是「不是经典」而不是「当季新番」，
        #    差了十几年。按季度算才对得上「每季 50–80 部里挑」这个核心场景。
        lo, hi = season_window()
        today = datetime.datetime.now(datetime.UTC).date().toordinal()
        keep &= (cat.air_ord >= lo.toordinal()) & (cat.air_ord <= hi.toordinal())
        if mode == "aired":
            keep &= cat.air_ord <= today
        elif mode == "upcoming":
            keep &= cat.air_ord > today
    # 只剔除「看过」和「不感兴趣」的；「想尝试」要留在推荐里
    rated = [r.subject_id for r in (Rating.coerce(x) for x in ratings)
             if r.exclude]
    keep &= ~np.isin(cat.ids, rated)

    idx = np.flatnonzero(keep)
    # ⚠️ **必须 stable。** 默认的 quicksort 对并列项给出任意顺序，而实测大量
    #    作品 tag 集合完全相同、余弦也完全相同（Top5 曾是 0.424/0.424/0.424/
    #    0.423/0.422）。不稳定排序有两个后果：结果不可复现（第 5 周评测要求
    #    可复现），以及与 recommend_sql 的 `ORDER BY match DESC, subject_id`
    #    对不上。idx 由 flatnonzero 产生（升序），cat.ids 又按 subject_id 排过，
    #    所以 stable 排序在并列时正好给出 subject_id 升序 —— 与 SQL 侧一致。
    order = idx[np.argsort(-sims[idx], kind="stable")]
    # 两段式排序时第一段要多召回一些，否则质量排序无从选起
    want = top_k if rank_by == "match" else max(top_k * DEFAULT_POOL_FACTOR,
                                                DEFAULT_POOL_MIN)

    def finish(picked: list[tuple[int, int, str, float]]) -> list[Recommendation]:
        """第二段：按贝叶斯加权评分重排。

        入参每项是 (展示作品在 cat 中的行号, subject_id, 展示名, 匹配度)。
        ⚠️ 行号必须随 picked 一起传进来，不能在这里用 index_of 反查 ——
           那是对 11k 长数组的线性扫描，召回池 200 条就是 200 次全表扫。

        ⚠️ 为什么需要这一步：平均每部只有 3.8 个非零维 / 308 维，大量作品的
           tag 集合**完全相同**，余弦也就完全相同 —— 实测 Top5 是
           0.424/0.424/0.424/0.423/0.422，差在小数点后第三位。
           召回那步已改成 stable 排序（并列按 subject_id 升序），所以顺序
           **可复现**，但「谁该排前面」在 tag 模型里依然没有依据 ——
           可复现不等于正确，subject_id 小只是入库早。
           先按匹配度召回、再按质量排，才能在「同样匹配」的一批里挑出好看的。

        ⚠️ 用 wr 而不是原始 score：5 人打 9.2 的冷门条目会盖过 3 万人打 9.2 的神作。
        """
        if not picked:
            return []
        ms = np.array([p[3] for p in picked], dtype=np.float64)
        qs = np.array([cat.wr[p[0]] for p in picked], dtype=np.float64)

        if rank_by == "match":
            final = ms
        elif rank_by == "quality":
            final = qs
        else:
            # blend：两个量纲差得远（余弦 0.2–0.45，评分 4–9），
            # 各自在候选池内 min-max 归一化到 [0,1] 再加权。
            # 用池内极值而非全局极值 —— 全局范围会把池内的差异压扁，
            # 比如池内余弦全在 0.30–0.42，按全局 [0,1] 归一化后差异几乎消失。
            def unit(v: np.ndarray) -> np.ndarray:
                lo, hi = v.min(), v.max()
                return np.full_like(v, 0.5) if hi - lo < 1e-9 else (v - lo) / (hi - lo)

            final = blend_alpha * unit(ms) + (1 - blend_alpha) * unit(qs)

        # stable：分数并列时保持第一段（匹配度）的先后，与旧实现一致
        order2 = np.argsort(-final, kind="stable")[:top_k]
        return [Recommendation(subject_id=picked[j][1], name=picked[j][2],
                               match=float(ms[j]), quality=float(qs[j]),
                               rank_score=float(final[j]))
                for j in order2]

    if not fold_series:
        return finish([(int(i), int(cat.ids[i]), cat.name[i], float(sims[i]))
                       for i in order[:want]])

    # 系列去重：同一系列只出一条，且换成该系列里用户还没作答过的最早一部。
    # 实测「厨力全开」档案的 6 条推荐只覆盖 3 个系列 —— 一人之下 ×3、
    # 东京喰种 ×2，而且推的全是续作。给没看过第一季的人推第六季毫无意义。
    smap = series.load(required=False)
    root_of = {int(s): smap.get(int(s), int(s)) for s in cat.ids}
    members: dict[int, list[int]] = {}
    for pos, sid in enumerate(cat.ids):
        members.setdefault(root_of[int(sid)], []).append(pos)

    out: list[tuple[int, int, str, float]] = []
    seen_roots: set[int] = set()
    for i in order:
        root = root_of[int(cat.ids[i])]
        if root in seen_roots:
            continue
        seen_roots.add(root)
        # ⚠️ 分数取**系列内最佳成员**的，而不是替代品自己的。
        #    排序依据必须和返回值一致，否则列表会不再降序 ——
        #    实测替代后出现过 0.329 夹在 0.513 和 0.489 之间。
        #    语义是「这个系列与你的口味有多匹配」，返回的则是入口作品。
        rank_score = float(sims[i])
        # ⚠️ 替代品必须**同样**通过全部过滤条件，直接复用 keep 掩码 ——
        #    它已包含 nsfw、mode、已作答、零向量四项。手写独立条件漏过一次：
        #    匹配到「超电磁炮T(2020)」却替换成「超电磁炮(2009)」，
        #    在 recent 模式下返回了 2011 年前的作品。
        cand = [p for p in members[root] if keep[p]]
        if cand:
            # 同系列里挑用户没答过的最早一部作为入口；同年则取热度高的
            i = min(cand, key=lambda p: (cat.year[p], -cat.done[p]))
        # ⚠️ quality 取**入口作品**的（i 已被替换），match 取系列最佳的。
        #    两者语义本就不同：「这个系列多对你口味」vs「这一部口碑如何」。
        out.append((int(i), int(cat.ids[i]), cat.name[i], rank_score))
        if len(out) >= want:
            break
    return finish(out)

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

from src import series, tag_rules
from src.textproc import keep_tags

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


def _clean(names, counts, vocab: frozenset[str]) -> dict[str, float]:
    """把一组原始 tag 名归一化、分流、并到词表维度上。"""
    out: dict[str, float] = {}
    for nm, ct in zip(names, counts, strict=True):
        canon = tag_rules.normalize(nm)
        if canon not in vocab or tag_rules.classify(canon) != "KEEP":
            continue
        out[canon] = out.get(canon, 0.0) + float(ct)
    return out


def build_catalog(conn: psycopg.Connection,
                  weighting: Weighting = "logtf-idf") -> Catalog:
    """从库里拉出全部作品，构建 (n, d) 的 tag 矩阵。

    权重：log(1+count) * idf，再按行 L2 归一化。
      · log —— count 随作品热度缩放（done=50 的作品 tag count 是个位数，
        done=50,000 的是几千），不压缩量级的话热门作品会主导一切
      · idf —— 「漫画改」覆盖 3,798 部，几乎不携带口味信息，应当降权
      · L2 —— 余弦的前提；同时抵消「tag 多的作品模长更大」

    binary 模式留给第 5 周做 ablation（第 10 节的可选对照）。
    """
    vocab = keep_tags()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT subject_id, COALESCE(name_cn, name), air_year, nsfw, fav_done,
                   tags, meta_tags,
                   COALESCE(EXTRACT(MONTH FROM air_date), 0)::int,
                   COALESCE(score, 0)::float, COALESCE(score_count, 0)::int,
                   air_date
            FROM anime_profile
            ORDER BY subject_id
        """)
        rows = cur.fetchall()

    per_work: list[dict[str, float]] = []
    ids, years, nsfws, dones, names, yms = [], [], [], [], [], []
    scores, votes, ords = [], [], []
    for sid, nm, yr, nsfw, done, tags, metas, mon, sc, vt, adate in rows:
        tags = tags or []
        w = _clean([t["name"] for t in tags], [t.get("count") or 0 for t in tags],
                   vocab)
        # meta_tags 没有票数。用该作品 tag 票数的中位数 —— 官方标签的可信度
        # 不低于用户 tag，但也不该压过最高票的那个。作品一个 tag 都没有时
        # 退化为 1，此时向量各维等权，L2 归一化后依然可用。
        base = float(np.median(list(w.values()))) if w else 1.0
        for m in _clean(metas or [], [base] * len(metas or []), vocab).items():
            w.setdefault(m[0], m[1])
        per_work.append(w)
        ids.append(sid)
        years.append(yr)
        yms.append((yr or 0) * 100 + (mon or 0))
        nsfws.append(nsfw)
        dones.append(done)
        names.append(nm)
        scores.append(sc)
        votes.append(vt)
        ords.append(adate.toordinal() if adate else 0)

    vlist = sorted(vocab)
    col = {t: i for i, t in enumerate(vlist)}
    mat = np.zeros((len(rows), len(vlist)), dtype=np.float32)
    for i, w in enumerate(per_work):
        for t, c in w.items():
            mat[i, col[t]] = 1.0 if weighting == "binary" else np.log1p(c)

    if weighting == "logtf-idf":
        dfreq = (mat > 0).sum(axis=0)
        idf = np.log((len(rows) + 1) / (dfreq + 1)).astype(np.float32)
        mat *= idf

    norm = np.linalg.norm(mat, axis=1, keepdims=True)
    norm[norm == 0] = 1.0        # 零向量保持为零，不产生 nan
    mat /= norm

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
          fold_series: bool = True,
          rank_by: RankBy = "blend",
          blend_alpha: float = DEFAULT_ALPHA,
          top_k: int = 20) -> list[tuple[int, str, float]]:
    """无状态打分。返回 [(subject_id, 展示名, 匹配度)]。

    第三项恒为**匹配度**（偏好向量与作品向量的余弦，范围 [-1,1]，
    正 = 比此人平均口味更对味）。

    ⚠️ **列表按 `rank_by` 指定的顺序排列，不一定按第三项降序。**
       rank_by="match"   → 顺序 == 匹配度降序
       rank_by="quality" → 顺序 == 贝叶斯加权评分降序
       rank_by="blend"   → 顺序 == α·匹配 + (1-α)·评分（池内归一化后）降序
       后两种模式下第三项会出现「大小交错」，这是预期的，不是 bug。
       消费方**直接按返回顺序展示**，不要拿第三项重新排序。

    📌 写 FastAPI 时先定：是继续返回三元组，还是改成带
       {match, quality, rank_score} 三个字段的结构体。后者更适合 JSON
       序列化，也能消掉上面这条容易踩的约定 —— 现在没有消费方，改动成本最低。
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
    order = idx[np.argsort(-sims[idx])]
    # 两段式排序时第一段要多召回一些，否则质量排序无从选起
    want = top_k if rank_by == "match" else max(top_k * DEFAULT_POOL_FACTOR,
                                                DEFAULT_POOL_MIN)

    def finish(picked: list[tuple[int, str, float]]) -> list[tuple[int, str, float]]:
        """第二段：按贝叶斯加权评分重排。

        ⚠️ 为什么需要这一步：平均每部只有 3.8 个非零维 / 308 维，大量作品的
           tag 集合**完全相同**，余弦也就完全相同 —— 实测 Top5 是
           0.424/0.424/0.424/0.423/0.422，差在小数点后第三位，
           它们之间的先后实际上由 argsort 的稳定性决定，等于随机。
           先按匹配度召回、再按质量排，才能在「同样匹配」的一批里挑出好看的。

        ⚠️ 用 wr 而不是原始 score：5 人打 9.2 的冷门条目会盖过 3 万人打 9.2 的神作。
        """
        if rank_by == "match" or not picked:
            return picked[:top_k]
        pos = {sid: n for n, (sid, _, _) in enumerate(picked)}
        if rank_by == "quality":
            return sorted(
                picked, key=lambda x: (-cat.wr[cat.index_of(x[0])], pos[x[0]])
            )[:top_k]

        # blend：两个量纲差得远（余弦 0.2–0.45，评分 4–9），
        # 各自在候选池内 min-max 归一化到 [0,1] 再加权。
        # 用池内极值而非全局极值 —— 全局范围会把池内的差异压扁，
        # 比如池内余弦全在 0.30–0.42，按全局 [0,1] 归一化后差异几乎消失。
        ms = np.array([x[2] for x in picked], dtype=np.float64)
        qs = np.array([cat.wr[cat.index_of(x[0])] for x in picked],
                      dtype=np.float64)

        def unit(v: np.ndarray) -> np.ndarray:
            lo, hi = v.min(), v.max()
            return np.full_like(v, 0.5) if hi - lo < 1e-9 else (v - lo) / (hi - lo)

        final = blend_alpha * unit(ms) + (1 - blend_alpha) * unit(qs)
        order2 = np.argsort(-final, kind="stable")
        return [picked[j] for j in order2[:top_k]]

    if not fold_series:
        return finish([(int(cat.ids[i]), cat.name[i], float(sims[i]))
                       for i in order[:want]])

    # 系列去重：同一系列只出一条，且换成该系列里用户还没作答过的最早一部。
    # 实测「厨力全开」档案的 6 条推荐只覆盖 3 个系列 —— 一人之下 ×3、
    # 东京喰种 ×2，而且推的全是续作。给没看过第一季的人推第六季毫无意义。
    smap = series.load(required=False)
    root_of = {int(s): smap.get(int(s), int(s)) for s in cat.ids}
    members: dict[int, list[int]] = {}
    for pos, sid in enumerate(cat.ids):
        members.setdefault(root_of[int(sid)], []).append(pos)

    out: list[tuple[int, str, float]] = []
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
        out.append((int(cat.ids[i]), cat.name[i], rank_score))
        if len(out) >= want:
            break
    return finish(out)

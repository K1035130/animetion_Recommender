-- 第 1 周 schema：alias + anime_profile
-- plot_chunk 按计划本周不建（第 4 周批次 2 时再加）
-- 幂等：可重复执行

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- anime_profile —— 每部动画一条
-- ============================================================
CREATE TABLE IF NOT EXISTS anime_profile (
    -- 直接用 Bangumi subject id 作主键：灌库幂等、季度增量同步不会撞行
    subject_id      integer PRIMARY KEY,

    name            text NOT NULL,          -- 原名（多为日文）
    name_cn         text,                   -- 中文名，可能为空
    summary         text,                   -- 简介，第 3 周 embedding 的输入之一

    air_date        date,                   -- dump 中 83.7% 为完整 YYYY-MM-DD，其余为空
    air_year        smallint,               -- 冗余出来便于筛选/聚合
    form            text,                   -- TV / WEB / 剧场版 / OVA，取自 meta_tags
    platform        smallint,               -- 1=TV 2=OVA 3=剧场版 5=WEB 0=未设置

    -- R18：入库保留、默认过滤。判定只认 dump 的 nsfw 字段，不用 tag（tag 会误杀肉番向 TV 动画）
    nsfw            boolean NOT NULL DEFAULT false,

    score           real,                   -- Bangumi 均分
    score_count     integer,                -- score_details 求和得到的评分人数
    score_details   jsonb,                  -- 1–10 分布直方图，评测/mean-centered 用
    rank            integer,

    -- 热度：done 提成独立列，第 9 节聚类选代表作要按它排序（jsonb 内取值无法建索引）
    fav_done        integer NOT NULL DEFAULT 0,
    favorite        jsonb,                  -- wish/done/doing/on_hold/dropped 全量

    tags            jsonb,                  -- 清洗后的 [{name, count}]
    meta_tags       text[],                 -- 官方规范化标签，动画覆盖率 88.7%

    -- AniList 补充（动作 4 回填）
    anilist_id      integer,
    name_en         text,
    studios         text[],
    staff           jsonb,
    popularity      integer,

    -- Phase 2（海外 taste gap）的钩子：现在存，将来免得重跑全量 AniList 请求
    -- {"anilist": 123, "mal": 456}。mal_id 是跨站实体对齐最强的锚点
    external_ids    jsonb NOT NULL DEFAULT '{}'::jsonb,

    -- 向量：1024 维 float4。按设计不建 HNSW —— 7k 条暴力算余弦仅几毫秒且是精确检索
    vec             vector(1024),

    -- 中文 BM25：Neon 装不了 zhparser，正文必须在 Python 侧用 jieba 预分词后写入
    -- ⚠️ 建库与查询必须同一分词器 + 同一词典
    search_tsv      tsvector,

    cluster_id      smallint,               -- 第 5–6 周聚类选题回填

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_profile_year     ON anime_profile (air_year);
CREATE INDEX IF NOT EXISTS idx_profile_done     ON anime_profile (fav_done DESC);
CREATE INDEX IF NOT EXISTS idx_profile_form     ON anime_profile (form);
CREATE INDEX IF NOT EXISTS idx_profile_cluster  ON anime_profile (cluster_id);
CREATE INDEX IF NOT EXISTS idx_profile_tsv      ON anime_profile USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_profile_metatags ON anime_profile USING gin (meta_tags);
-- 默认过滤 nsfw 的部分索引：问卷选题走的就是这个口径
CREATE INDEX IF NOT EXISTS idx_profile_sfw_done ON anime_profile (fav_done DESC)
    WHERE NOT nsfw;

-- ============================================================
-- alias —— 动画与角色实体的别名表
-- ============================================================
CREATE TABLE IF NOT EXISTS alias (
    id                bigserial PRIMARY KEY,

    name              text NOT NULL,        -- 原始别名
    norm_name         text NOT NULL,        -- 归一化后（小写、去空格/标点）用于精确匹配

    entity_type       text NOT NULL CHECK (entity_type IN ('subject', 'character')),

    subject_id        integer REFERENCES anime_profile (subject_id) ON DELETE CASCADE,
    character_id      integer,

    -- ⚠️ 角色消歧必须锚定在已确认的 subject 范围内，否则跨作品同名角色会撞车
    parent_subject_id integer REFERENCES anime_profile (subject_id) ON DELETE CASCADE,

    source            text NOT NULL,        -- name / name_cn / infobox_alias / anilist / moegirl

    created_at        timestamptz NOT NULL DEFAULT now(),

    -- 同一实体下同名别名只留一条。
    -- ⚠️ 必须写 NULLS NOT DISTINCT（PG15+）：subject_id 和 character_id 里
    --    必有一个是 NULL（作品行没有 character_id，角色行没有 subject_id），
    --    而 Postgres 默认把 NULL 视为互不相等 —— 不加这句，约束对**每一行**
    --    都失效，重复别名会全部灌进去。已实测验证。
    CONSTRAINT alias_uniq UNIQUE NULLS NOT DISTINCT
        (entity_type, subject_id, character_id, norm_name)
);

CREATE INDEX IF NOT EXISTS idx_alias_norm   ON alias (norm_name);
CREATE INDEX IF NOT EXISTS idx_alias_parent ON alias (parent_subject_id);
-- 模糊匹配兜底：pg_trgm 对中文是字符级 trigram，实测相似度可用
--（'fate zeero' → Fate/Zero 相似度 0.727）。用于「名字拼错/简写」时的召回，
-- 不用于相关性排序。extension 保留 —— similarity() 函数本身需要它。
--
-- ⚠️ 索引**故意不建**（2026-08-11，为省空间）：它一个就要 9.9 MB，
--    而 alias 只有 3.8 万行，顺序扫描算 similarity 约几十毫秒，
--    对一个很少触发的兜底路径完全够用。将来若发现慢，一条 SQL 即可重建：
--
--    CREATE INDEX idx_alias_trgm ON alias USING gin (norm_name gin_trgm_ops);

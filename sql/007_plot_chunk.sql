-- 第 4 周：剧情/角色语料层 —— plot_chunk 及其作用域映射。
-- 幂等：可重复执行。
--
-- 前置：001_init.sql（要 anime_profile.subject_id 和 vector 扩展）。
--       与 002–006 无先后依赖。
--
-- 灌数据的脚本（尚未写）：
--   scripts/build_plot_chunks.py   萌娘作品页 19,526 chunk  ≈ 5 分钟 / ¥0.20
--   scripts/build_char_chunks.py   dump 角色简介 66,871 条  ≈ 15 分钟 / ¥0.46（零抓取）

-- ============================================================
-- 三处推翻了 CLAUDE.md 的原设计，理由都是实测
-- ============================================================
-- ① **正文进库**，不走 R2 的 content_ref。
--    原设计写于「计划 10 万 chunk」+「免费层 500 MB 是悬崖」两个前提之下，
--    两个前提现在都没了：实际 19,526 条，正文实测仅 10.5 MB；
--    Neon 已升级付费，存储是 $0.35/GB-月 的线性成本。
--    ⚠️ 而比省钱更硬的理由：**BM25 那条腿需要正文在 Postgres 里**。
--       E.4 明确要求歌名走 BM25，而本项目的 BM25 = search_tsv + jieba 预分词。
--       就算只灌 tsvector 不灌正文，tsvector 本身约 17 MB，与正文同量级 —— 省不下什么。
--
-- ② **不建 HNSW**。2026-08-16 实测（EXPLAIN ANALYZE，排除跨国传输）：
--       限定单系列（中位 7 条）        0.1 ms
--       跨作品全表暴力（外推 19,526）   77 ms
--    而流程 C 的每个请求都必然包含一次 embedding 往返（~500 ms）和一次 LLM 生成（数秒），
--    77 ms 在其中是噪声。HNSW 要付 28–56 MB 索引 + 建索引时间 + **近似检索会漏召回**，
--    而跨作品发现型查询恰恰最怕漏。
--    ⚠️ 这个决定可逆：真需要时一条 CREATE INDEX 就补上，不用重灌数据。
--    📌 与「11,453 部 profile 不建 HNSW」同一条判据，那条已被实测证实（≈0 ms）。
--
-- ③ **halfvec(1024) 而非 512**。512 同样是「chunk 层放不下」时的妥协，
--    第 5 节自己的话是「不值得为省几 MB 让掉 MRL 截断的 1–3% 质量」。
--    ⚠️ 决定性的理由不是存储而是**少一步变换**：查询向量原生 1024 维，
--       用 512 就必须在每次查询时客户端截断 + 重新归一化，且必须与建库时逐位一致。
--       这正是本项目反复吃亏的那类风险（jieba 词典漂移、series_root.json 不入 git、
--       %.7g vs %.9g）—— 不一致不报错，只是静默降低召回。
--    ⚠️ 可逆：缓存里永远存 1024（A.9），改维度是从缓存重放，零 API 成本约 20 分钟。
--       第 10 节的「512 vs 1024 ablation」照做不误。

-- ============================================================
-- moegirl_page —— 萌娘条目，每个 pageid 一行
-- ============================================================
-- ⚠️ title 放这里而**不是**复制到每条 chunk 上。它有三个用处，都在条目层：
--    拼 prompt（「以下是关于《X》的资料」）· 溯源 URL · CC BY-NC-SA 3.0 的署名要求（E.1）。
CREATE TABLE IF NOT EXISTS moegirl_page (
    pageid      integer     PRIMARY KEY,

    -- 'series' = 作品页（批次 2，已抓 2,233 个）
    -- 'character' = 角色页（批次 3，未抓）
    kind        text        NOT NULL CHECK (kind IN ('series', 'character')),

    title       text        NOT NULL,
    -- ⚠️ 重抓时靠它判断有没有更新，避免全量重跑。
    --    prop=info 一次给 50 个标题的 lastrevid，是标题解析顺带拿到的（E.2）。
    lastrevid   bigint      NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- plot_chunk —— 语料本体。两个来源共用一张表
-- ============================================================
-- ⚠️ **为什么不拆成两张表**：回答「XX 角色的结局」时要同时搜「该角色的 dump 简介」
--    「该角色的萌娘页」「该作品的剧情概要」。同一张表 = 一条 ORDER BY；
--    拆表就得 UNION ALL 再重排。而它们用的是同一个 embedding 模型、同一个维度，
--    本来就在同一个向量空间里 —— 分开存反而是人为制造麻烦。
--    📌 注意这和「episode.jsonlines 别混进 plot_chunk」不冲突：那条讲的是
--       别把不同**粒度**（分集 vs 剧情段）混进同一次灌库，不是禁止多来源共表。
CREATE TABLE IF NOT EXISTS plot_chunk (
    chunk_id      bigserial PRIMARY KEY,

    -- 'moegirl'      萌娘百科页面切出来的段落
    -- 'bangumi_char' dump 的 character.jsonlines 里的角色简介（零抓取）
    source        text      NOT NULL CHECK (source IN ('moegirl', 'bangumi_char')),

    -- ⚠️ 两个来源各有一个为 NULL —— 见下面 UNIQUE 约束的警告
    pageid        integer   REFERENCES moegirl_page (pageid) ON DELETE CASCADE,
    character_id  integer,

    chunk_no      integer   NOT NULL,

    -- 'prose'   散文（剧情概要、角色描述）→ 走向量检索
    -- 'songs'   OP/ED/插入曲            → **走 BM25 更准**（歌名是关键词查找，E.4）
    -- 'profile' dump 角色简介
    -- ⚠️ kind 不只是为了取舍，也是为了**按问题类型分流检索**。
    kind          text      NOT NULL CHECK (kind IN ('prose', 'songs', 'profile')),

    section       text,     -- 章节名，进 prompt 当标签（「剧情概要」/「主题曲」）
    section_id    text,     -- Parsoid 锚点，重抓时定位用

    -- 正文进库（见文件头 ①）。实测 19,526 条共 10.5 MB。
    text          text      NOT NULL,

    -- ── 剧透 ──────────────────────────────────────────────
    -- spoiler_level 是**判定结果**，下面两列是产生它的**原始信号**。
    -- ⚠️ 两个都存，因为**规则还会改** —— E.6 还挂着「章节级规则（『结局』
    --    『最终话』整节算剧透）等全量数据出来再定」。
    --    留着原始信号，改判定规则是一条 UPDATE；不留就得重新解析 2,233 个 HTML。
    --    两列一共几十 KB。这与「先落原始响应，解析作为独立阶段」是同一条思路。
    spoiler_level smallint  NOT NULL DEFAULT 0,
    heimu_chars   integer   NOT NULL DEFAULT 0,   -- heimu 行内标记的字数（精确但零散）
    spoiler_box   boolean   NOT NULL DEFAULT false, -- 「以下内容含有剧透成分」提示框（整段）

    vec           halfvec(1024),
    search_tsv    tsvector,

    created_at    timestamptz NOT NULL DEFAULT now(),

    -- ⚠️ **必须写 NULLS NOT DISTINCT（PG15+）**，与 alias 表同一个坑（001_init.sql）：
    --    moegirl 来源的 character_id 是 NULL，bangumi_char 来源的 pageid 是 NULL，
    --    而 Postgres 默认把 NULL 视为互不相等 —— 不加这句，约束对**每一行**都失效，
    --    重跑灌库脚本会把整批数据再插一遍且不报错。
    CONSTRAINT plot_chunk_uniq UNIQUE NULLS NOT DISTINCT
        (source, pageid, character_id, chunk_no),

    -- 来源与外键的对应关系，写成约束而不是靠脚本自觉
    CONSTRAINT plot_chunk_source_shape CHECK (
        (source = 'moegirl'      AND pageid IS NOT NULL) OR
        (source = 'bangumi_char' AND character_id IS NOT NULL AND pageid IS NULL)
    )
);

-- ============================================================
-- plot_chunk_scope —— 作用域映射（原则 4 的落地处）
-- ============================================================
-- 流程 C 的每次检索都必须限定在已确认的作品范围内，否则跨作品同名角色会撞车。
-- 这张表就是那个「范围」。
--
-- ⚠️ **2026-08-16 修正：键从 pageid 改成 chunk_id。**
--    原设计是 (series_root, pageid)，只有 2,281 行、很省。但它**覆盖不了
--    dump 角色简介** —— 那批没有 pageid，作用域要走
--    subject-characters ⋈ anime_profile.series_root。
--    两个来源各一张映射表会让检索变成 UNION，而按 chunk 建映射实测同一量级
--    （≈ 100k 行 / 几 MB），却让所有来源共用一条 JOIN。
--
-- ⚠️ **这是派生表，由灌库脚本重建，不要手工编辑。** 各来源的推导路径：
--      moegirl       moegirl_chunks.jsonl 的 series_roots 字段
--      bangumi_char  subject-characters → anime_profile.series_root
--
-- 💡 一个条目可以服务多个系列根（猎人 1999/2011 → 同一个萌娘页），
--    所以是多对多，不能在 plot_chunk 上放一列 series_root。
CREATE TABLE IF NOT EXISTS plot_chunk_scope (
    series_root integer NOT NULL REFERENCES anime_profile (subject_id) ON DELETE CASCADE,
    chunk_id    bigint  NOT NULL REFERENCES plot_chunk (chunk_id)      ON DELETE CASCADE,
    -- ⚠️ series_root 在前：查询永远从它出发，这个顺序决定了 PK 索引能不能用上
    PRIMARY KEY (series_root, chunk_id)
);

-- ============================================================
-- 索引 —— 只有两个，**没有 HNSW**
-- ============================================================
-- 作用域映射的反向查找（删页面、重建 scope 时用）
CREATE INDEX IF NOT EXISTS idx_chunk_scope_chunk ON plot_chunk_scope (chunk_id);

-- 灌库/重建时按来源定位
CREATE INDEX IF NOT EXISTS idx_plot_chunk_page  ON plot_chunk (pageid);
CREATE INDEX IF NOT EXISTS idx_plot_chunk_char  ON plot_chunk (character_id);

-- BM25 那条腿。⚠️ search_tsv 必须在 Python 侧用 jieba 预分词后
--    to_tsvector('simple', tokenize(text)) 写入 —— Neon 装不了 zhparser，
--    Postgres 内置分词器切不了中文（第 6 节实测）。
--    ⚠️ 查询端必须用**同一套词典**，靠 server/main.py 的 BUILD_FINGERPRINT 校验。
CREATE INDEX IF NOT EXISTS idx_plot_chunk_tsv   ON plot_chunk USING gin (search_tsv);

-- ============================================================
-- ⚠️ 灌库时的三条纪律（都踩过）
-- ============================================================
-- 1. **三段式连接**：读库 → 长耗时 API → 写库，每段各开各的连接。
--    Neon 是 serverless，空闲连接会被回收；第 3 周握着连接跑完 11 分钟的 API 阶段，
--    写库时 SSL connection has been closed unexpectedly。
--    scripts/build_embeddings.py 的写法可以照抄。
--
-- 2. **分批灌 + 每批后普通 VACUUM，不要 VACUUM FULL。**
--    它重写整张表、产生等于表大小的 WAL，而 WAL 进 Neon 的存储计量
--    （第 3 周两次 VACUUM FULL 就贡献了约 150 MB）。普通 VACUUM 不重写页面。
--
-- 3. **判断成败不能走管道**：`cmd | tail` 会把退出码换成 tail 的，
--    后台任务因此报过 exit code 0 而实际是失败的。重定向到文件再看 $?。

-- ============================================================
-- 验收
-- ============================================================
-- 建表后：
--   \d plot_chunk
--   -- 预期：vec 是 halfvec(1024)；索引只有 pkey / uniq / page / char / tsv，**无 hnsw**
--
-- 灌完萌娘作品页（阶段 02）：
--   SELECT source, kind, count(*), count(vec), count(search_tsv)
--     FROM plot_chunk GROUP BY 1, 2 ORDER BY 1, 2;
--   -- 预期 moegirl/prose 16,354 · moegirl/songs 3,172，vec 与 search_tsv 全非空
--
--   SELECT count(*) FROM moegirl_page;                    -- 预期 2,232（《弹珠汽水》
--                                                         --   是 251 字节的小作品页，
--                                                         --   解析出零 chunk，不建行）
--   SELECT count(DISTINCT series_root) FROM plot_chunk_scope;  -- 预期 2,281
--
-- 灌完 dump 角色简介（阶段 03）：
--   SELECT count(*) FROM plot_chunk WHERE source = 'bangumi_char';   -- 预期 ≈ 66,871
--   SELECT count(*) FROM plot_chunk WHERE source = 'bangumi_char' AND character_id IS NULL;
--   -- 预期 0
--
-- 检索能跑通（流程 C 的常规路径，限定单个系列）：
--   EXPLAIN ANALYZE
--   SELECT c.text, c.section, c.kind
--     FROM plot_chunk_scope s JOIN plot_chunk c USING (chunk_id)
--    WHERE s.series_root = 265          -- 新世纪福音战士
--      AND c.spoiler_level = 0          -- 剧透门控
--    ORDER BY c.vec <#> (SELECT vec FROM plot_chunk WHERE chunk_id = 1)::halfvec
--    LIMIT 8;
--   -- 预期：走 plot_chunk_scope 的 PK 索引，扫几十行，个位数毫秒

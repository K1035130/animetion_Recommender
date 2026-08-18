-- 译文备份表 —— 日译中结果的持久副本。
-- 幂等：可重复执行。
--
-- 前置：无（独立于其他表）。
--
-- ============================================================
-- 为什么要这张表（2026-08-17 Kevin 批准约 20 MB）
-- ============================================================
-- 译文的**工作副本**在本地 SQLite（data/interim/translate_cache/），
-- loader 也是从那里读。那为什么还要入库？
--
--   编码一次全量  ¥0.19 / 12 分钟   —— 丢了重来还能忍
--   翻译一次全量  47,066 条 / 数小时 —— 丢了重来是一天
--
-- 而 data/interim/ **不入 git**（.gitignore 只放行 tag_vocab.json）。
-- 换机器 / 清盘 / 误删，本地缓存就没了。⇒ 库里存一份，restore 回来是几秒钟。
--
-- ⚠️ **这张表不在任何打分或检索路径上。** 它纯粹是备份 ——
--    线上不读它，loader 也优先读本地 SQLite（免一次跨国往返）。
--    与 embed_cache 的定位一致：缓存是建库的输入，不是事实来源。
--
-- ============================================================
-- 键的设计
-- ============================================================
-- ⚠️ 主键是 **(src_sha, model, prompt_version)** 三元组，不是 src_sha 单列。
--    换模型或改 prompt 都会产生不同的译文，而它们必须能共存 ——
--    否则「换个模型重译一批做对比」这件事做不了，只能互相覆盖。
--    这与 embed_cache 把 MODEL/DIM 算进键是同一条理由（A.8 的静默失效）。
--
-- ⚠️ 存 src 原文而不只存哈希：事后审计、抽查译文质量、
--    以及万一要换 key 算法时能重放。几 MB 的事。
CREATE TABLE IF NOT EXISTS translation (
    src_sha        text NOT NULL,          -- sha256(源文本)
    model          text NOT NULL,
    prompt_version text NOT NULL,
    src            text NOT NULL,
    dst            text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT translation_pk PRIMARY KEY (src_sha, model, prompt_version)
);

-- 按模型统计/清理用。译文本身的查找一律走主键。
CREATE INDEX IF NOT EXISTS idx_translation_model ON translation (model);

-- ============================================================
-- 验收
-- ============================================================
--   \d translation
--   SELECT model, prompt_version, count(*), pg_size_pretty(sum(length(src)+length(dst))::bigint)
--     FROM translation GROUP BY 1, 2;
--   -- 预期：tencent/Hunyuan-MT-7B | v1 | 47,066 | 约 20 MB

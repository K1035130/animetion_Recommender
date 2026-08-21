-- ============================================================
-- 009 —— 声优与配役（回答「XXX 配过哪些角色」）
-- ============================================================
-- 数据来源：Bangumi dump 的 person.jsonlines + person-characters.jsonlines
--           + subject-characters.jsonlines（后者只提供角色重要度 type）
--
-- ⚠️ **为什么不抓萌娘声优页**（2026-08-21 实测对照，钉宫理惠）：
--      dump 701 条配对 / 433 部作品，带 subject_id + character_id，零抓取；
--      萌娘 749 条「角色————《作品》」/ 710 部，只有条目名，**要做标题映射**
--      （F.5 那类问题的新一份），且要抓约 2,500 页 ≈ 5 小时。
--    多出来的那 48 条基本是游戏 / Drama CD，对动画问答价值有限。
--    ⇒ 先用 dump 把功能做出来；若上线后「角色名显示成日文」真的影响体验
--      （dump 的角色中文名覆盖 66.8%），再定向抓萌娘补名，那时是可量化的补丁。
--
-- ⚠️ **不做截断。** 这是问答不是推荐特征：
--    「花泽香菜配过哪些角色」只答主角役就是答错。role_type 存下来**只用于排序**
--    （主角排前面），而不是过滤 —— 截断丢的数据找不回来，排序不丢。
--    📌 推荐侧的声优特征（staff_vec）是另一件事，那里才需要「仅主角 + df>=8」，
--       且要改 sql/006 的列宽与 staffvec.DIM，风险与时机都不同，别混做一次。
-- ============================================================

BEGIN;

-- ── person —— 只灌声优，但表本身不限职业 ────────────────────
-- ⚠️ career 整个存下来：dump 里一个人可以既是 seiyu 又是 artist（水树奈奈），
--    将来要按职业筛选时不用重灌。
CREATE TABLE IF NOT EXISTS person (
    person_id  integer PRIMARY KEY,
    name       text    NOT NULL,          -- dump 原名，多为日文
    name_cn    text,                      -- infobox 的「简体中文名」，声优里 76.9% 有
    career     text[]  NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ── voice_role —— 配役三元组 ────────────────────────────────
-- ⚠️ character_id **不设外键**：dump 的角色覆盖面比我们的候选集大，
--    而 alias 表里只有候选集内作品的角色行。设外键会在灌库时炸一批，
--    而那些行本身是对的（只是我们没收录那部作品的角色）。
--    查角色名时 LEFT JOIN alias，查不到就显示 dump 的原名。
CREATE TABLE IF NOT EXISTS voice_role (
    person_id    integer  NOT NULL REFERENCES person (person_id) ON DELETE CASCADE,
    subject_id   integer  NOT NULL REFERENCES anime_profile (subject_id) ON DELETE CASCADE,
    character_id integer  NOT NULL,
    -- 1=主角 2=配角 3=客串（来自 subject-characters.type）。可能为 NULL：
    -- person-characters 里有配对，而 subject-characters 里没有对应行。
    role_type    smallint,
    PRIMARY KEY (person_id, subject_id, character_id)
);

CREATE INDEX IF NOT EXISTS idx_voice_role_subject   ON voice_role (subject_id);
CREATE INDEX IF NOT EXISTS idx_voice_role_character ON voice_role (character_id);

-- ── alias 扩展：让声优名走同一个「名字 → 实体」入口 ──────────
-- ⚠️ **不新建 person_alias 表**：名字解析必须只有一个定义处。
--    resolve() 将来要认声优名时，改的是数据不是代码路径。
ALTER TABLE alias ADD COLUMN IF NOT EXISTS person_id integer
    REFERENCES person (person_id) ON DELETE CASCADE;

ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_entity_type_check;
ALTER TABLE alias ADD CONSTRAINT alias_entity_type_check
    CHECK (entity_type IN ('subject', 'character', 'person'));

-- 🚨 **person_id 必须进唯一约束。** 声优行的 subject_id 和 character_id 都是 NULL，
--    而 alias_uniq 是 NULLS NOT DISTINCT —— 不加的话，两个同名声优
--    （('person', NULL, NULL, '同名')）会撞约束，后灌的那个被静默丢掉。
ALTER TABLE alias DROP CONSTRAINT IF EXISTS alias_uniq;
ALTER TABLE alias ADD CONSTRAINT alias_uniq UNIQUE NULLS NOT DISTINCT
    (entity_type, subject_id, character_id, person_id, norm_name);

CREATE INDEX IF NOT EXISTS idx_alias_person ON alias (person_id);

COMMIT;

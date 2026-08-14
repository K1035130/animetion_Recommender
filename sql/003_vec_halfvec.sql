-- 第 3 周前置：把 embedding 列从 vector(1024) 改成 halfvec(1024)。
-- 幂等：可重复执行。
--
-- ⚠️ **必须在灌数据之前跑。** 现在这一列是 001_init.sql 建的、全 NULL，
--    改类型是一条 ALTER；灌完 11,453 条再改要重灌一遍。
--
-- 前置：001_init.sql（建 anime_profile.vec）、002_tag_vec.sql（建 tag_vec）。

-- ============================================================
-- anime_profile.vec —— Qwen3-Embedding 的 summary 向量
-- ============================================================
-- 维度 1024，与 plot_chunk 的 512 **刻意不统一**（见第 5 节）：
--   profile 11,453 条，1024→512 只省 11.7 MB（预算的 2.3%），
--   不值得为此让掉 MRL 截断的 1–3% 质量；
--   plot_chunk 10 万条，1024 要 445 MB —— 512 是唯一放得进预算的选择。
-- 两者从不互相比较（各自只和查询向量比），所以维度不同不成问题。
--
-- ⚠️ **fp16 而非 fp32，与隔壁 tag_vec 的选择相反 —— 这不是不一致，是两回事。**
--    002_tag_vec.sql 给 tag_vec 选 vector(308) 的理由是：大量作品 tag 集合
--    完全相同、余弦并列到小数点后三位，fp16 会把这些**精确并列**变成随机抖动，
--    排序不可复现。
--    embedding 没有这个性质 —— 1024 维稠密连续向量，两部作品向量完全相同
--    实际上不可能（除非简介逐字相同）。并列问题不存在，所以 fp16 的
--    23.5 MB（相对 fp32 的 47 MB）省得干净。
--
-- ⚠️ **但 parity 有一条必须守住的纪律**（tests/test_parity.py：TOL=1e-5，
--    SWAP_TOL=1e-4）：fp16 的相对精度约 1e-3，**远大于这两个容差**。
--    所以 —— **两条打分路径必须都从这一列读，不能一条读库、一条读缓存。**
--    embedding 缓存存的是 API 返回的 fp32 原值（见 A.9），
--    numpy 评测路径若图省事直接读缓存文件，就会拿 fp32 去比 SQL 的 fp16，
--    差异约 1e-3，直接冲破 SWAP_TOL —— 而且飘得不大不小，
--    最容易被当成「浮点误差」放过去。
--    ⬜ P1 落地、parity 测试覆盖融合路径时，这一条要单独验一遍。
--
-- ⚠️ 万一 parity 真的守不住，回退成本很低：ALTER 回 vector(1024) + 从缓存重灌，
--    **不需要重新请求 API**。这正是 A.9 要求缓存存完整 fp32 的原因之一。
--
-- ⚠️ 589 部 summary 为空（5.1%）→ **存 NULL，不存零向量**。
--    与 tag_vec 同一条理由（002 第 30 行）：零向量与任何偏好向量的余弦都是 0，
--    而偏好向量整体为负时 0 反而**高于**所有负相关作品。
--    存 NULL 才能用 `vec IS NOT NULL` 一句话过滤掉。
DO $$
DECLARE
    cur_type text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod)
      INTO cur_type
      FROM pg_attribute a
      JOIN pg_class     c ON c.oid = a.attrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relname  = 'anime_profile'
       AND n.nspname  = current_schema()
       AND a.attname  = 'vec'
       AND NOT a.attisdropped;

    IF cur_type IS NULL THEN
        -- 理论上走不到：001_init.sql 一定建过这一列。留着是为了让本文件
        -- 在只跑过 002 的库上也能自愈，而不是报一个含糊的 "column does not exist"。
        ALTER TABLE anime_profile ADD COLUMN vec halfvec(1024);
        RAISE NOTICE 'anime_profile.vec 不存在，已新建为 halfvec(1024)';

    ELSIF cur_type = 'halfvec(1024)' THEN
        RAISE NOTICE 'anime_profile.vec 已经是 halfvec(1024)，跳过';

    ELSE
        -- ⚠️ 必须写 USING：pgvector 的 vector → halfvec 不是隐式转换。
        --    当前列全 NULL，所以这一步不损失任何已有数据；
        --    若将来在有数据时重跑，fp32 → fp16 是**不可逆的降精度**，
        --    要恢复只能从 embedding 缓存重灌。
        EXECUTE 'ALTER TABLE anime_profile ALTER COLUMN vec TYPE halfvec(1024) '
                'USING vec::halfvec(1024)';
        RAISE NOTICE 'anime_profile.vec: % → halfvec(1024)', cur_type;
    END IF;
END $$;

-- ============================================================
-- 索引：故意不建
-- ============================================================
-- ⚠️ **不建 HNSW**，与第 4 节对 profile 向量的判断一致：
--    11,311 行暴力算余弦实测 ≈ 0 ms，且是精确检索，比近似索引召回更准。
--    HNSW 在 1024 维下会额外吃掉约 28 MB —— 第 5 节预算里 chunk 层才是大头，
--    这里花掉的每 MB 都是从 chunk 的 15 万条天花板上扣的。
--    plot_chunk 建 HNSW 是因为它有 10 万条且要 ANN，两者场景不同。
--
-- 也不建 `vec IS NOT NULL` 的部分索引：11,453 行里非空的预计 10,864 行
-- （11,453 − 589 空 summary），选择性太低，规划器多半仍走顺序扫描。
-- 理由与 002 末尾对 tag_vec 的判断相同。

-- ============================================================
-- 验收
-- ============================================================
-- 跑完本文件后应看到 halfvec(1024) / 0 行非空：
--
--   SELECT format_type(atttypid, atttypmod) AS vec_type
--     FROM pg_attribute
--    WHERE attrelid = 'anime_profile'::regclass AND attname = 'vec';
--
--   SELECT count(*) FILTER (WHERE vec IS NOT NULL) AS with_vec,
--          count(*)                                AS total
--     FROM anime_profile;
--
-- ⚠️ 灌完向量之后记得 `VACUUM FULL anime_profile` —— 批量 UPDATE 的
--    MVCC 膨胀实测能让库虚涨一倍（第 5 节）。
--    但第 4 周对 plot_chunk **不要**这么干，那时库已到 324 MB，
--    全表重写会短暂占用两倍空间，可能顶穿 500 MB（Neon 超限是挂起项目）。

-- 第 2 周末：把打分所需的两样东西搬进库，让线上打分不再依赖常驻内存。
-- 幂等：可重复执行。
--
-- 背景（2026-08-12 决定）：放弃 Render 常驻进程，改上 Vercel serverless。
-- 无状态函数里没有 lifespan，Catalog 不可能常驻；每请求重建要拉 2.6 MB / 1.3 s，
-- 不可接受。解法是把矩阵乘法推进 Postgres —— 1.1 万条暴力算余弦本来就只要几毫秒
-- （第 4 节「不建 HNSW」的判断在这里同样成立）。

-- ============================================================
-- tag_vec —— 清洗后的 308 维题材 tag 向量
-- ============================================================
-- ⚠️ **这一列是 tag 向量的唯一定义处。**
--    在此之前向量是 recommend.build_catalog() 每次在 Python 里重算的
--    （log1p(count) × idf，再行 L2 归一化）。改成离线算一次写进来之后，
--    线上 SQL 打分与第 5 周评测的 numpy 批量打分**读的是同一批数字** ——
--    两套口径一致成了构造上保证的事，不再依赖两份实现保持同步。
--    重算逻辑在 scripts/build_tag_vectors.py，改动那里必须重跑本列。
--
-- ⚠️ idf 依赖全库词频，因此向量**只能全量重算，不能逐行增量更新**。
--    第 6 周季度同步加入新作品后，必须整列重跑一遍。
--
-- ⚠️ 用 vector(308) 而不是 halfvec(308)，与第 4 节给 plot_chunk 选 halfvec 不同。
--    理由是**可验证性**：numpy 侧是 float32，fp16 会引入舍入差异，
--    而线上 SQL 路径与评测 numpy 路径的一致性正是靠逐条比对余弦来验证的
--    （tests/test_parity.py）。差 7 MB 换一个能断言的不变式，划算。
--    实测大量作品 tag 集合完全相同、余弦并列到小数点后三位，
--    fp16 会让这些并列变成随机抖动，排序不可复现。
ALTER TABLE anime_profile ADD COLUMN IF NOT EXISTS tag_vec vector(308);

-- 零向量作品（142 部，多为欧美动画与国产老动画）**存 NULL 不存零向量**。
-- 它们与任何偏好向量的余弦都是 0，而偏好向量整体为负时 0 反而高于所有
-- 负相关作品 —— 必须排除在候选之外。存 NULL 才能用 `tag_vec IS NOT NULL`
-- 一句话过滤掉，存零向量则要写成 `tag_vec <> '[0,0,...]'`，既丑又慢。

-- ============================================================
-- series_root —— 续作 → 系列第一部
-- ============================================================
-- 原先在 data/interim/series_root.json，由 src/series.py 读取。
-- ⚠️ 搬进库有两个理由，缺一不可：
--    1. serverless 下每请求读一次文件 + 解析 3,143 条 JSON 不可行（实测 1.1 ms/次）
--    2. 原来的路径 Path("data/interim/series_root.json") 是**相对 CWD** 的，
--       而 score() 走 load(required=False) —— 工作目录不对时不报错，
--       只是续作折叠**静默关闭**（实测切到 C:/ 后返回 0 条映射）。
--       表现是「推荐列表里突然全是第二季第三季」，日志里一个字都没有。
--
-- 默认等于自身：不是续作的作品，其系列根就是它自己。
-- 这样 GROUP BY / DISTINCT ON series_root 不需要 COALESCE。
ALTER TABLE anime_profile ADD COLUMN IF NOT EXISTS series_root integer;

-- 召回阶段按 series_root 去重（DISTINCT ON），需要它排在前面
CREATE INDEX IF NOT EXISTS idx_profile_series_root ON anime_profile (series_root);

-- 打分查询的复合过滤条件。nsfw / score / tag_vec 三项每次都要过，
-- 而 11,453 行里 tag_vec IS NOT NULL 的有 11,311 行 —— 选择性不高，
-- 规划器多半仍走顺序扫描。**故意不建复合索引**：
-- 全表算 11k 次余弦本来就是毫秒级，索引只会白占空间（第 5 节预算吃紧）。

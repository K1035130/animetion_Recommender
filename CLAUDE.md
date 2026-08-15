# 动画推荐系统 — 项目前情提要

> 本文档为项目决策记录与执行计划，供 Claude Code 作为初始上下文引入。
> 所有技术选型均已论证完毕，**除非有新证据，不要重新讨论已决事项**。

---

## 🧭 怎么读这份文档

文档分五个部分，**信息新旧不同，冲突时按下面的优先级判断谁说了算**：

| 部分 | 内容 | 时效 |
|---|---|---|
| **一** | 现状与下一步 | 最新，每次干完活就更新 |
| **二** | 系统怎么运转（操作手册） | 最新，**实操以这里为准** |
| **三** | 已完工的论证（勿回退） | 已定案，附实测依据 |
| **四** | 待办 | 活的清单 |
| **五** | 设计文档 第 1–15 节 | **最初的设计稿，部分已被一、二部分推翻** |

⚠️ **第五部分是原始设计稿，不是当前状态。** 它保留下来是因为里面的**论证过程**仍然有用
（为什么否决 AWS、为什么 `done>=50` 是好阈值、口径怎么一步步收紧的），
但其中的**结论**可能已经被后来的实测推翻。已知被推翻的地方都在原处加了醒目标注。

⚠️ **第 1–15 节的编号是稳定锚点，不要重新编号。** 全文有几十处
「第 N 节」交叉引用依赖它。新增内容一律往一~四部分放，不要插进第五部分打乱编号。

📌 **Phase 2 的海外数据源接入方案已移出本文件** → [docs/phase2-overseas-data.md](docs/phase2-overseas-data.md)。
它有自己独立的第 1–9 节编号，混在一个文件里会让「第 4 节」变成歧义引用。
六周排期内不实现，但里面「现在就埋的两个钩子」已经落地。

---


# 第一部分 · 现状与下一步

## 📍 当前进度（更新于 2026-08-15）

**第 1–3 周全部完工。下一步是第 4 周：萌娘百科语料 + HyDE + 混合检索。**

第 3 周产出：Qwen3-Embedding 建库（10,864 部）· 问卷选题改 MMR 多样性序 ·
问卷支持多次作答 · P1 三路融合（tag + embedding + staff/studio）。

`animetion-recommender.vercel.app` —— 前端 + API 同一个 Vercel 项目、同源。
库占用 **110 MB / 500 MB**（Neon 控制台口径；`pg_database_size()` 报 86 MB，
差值是保留的历史，见第 5 节）。
测试 **28 项**（18 项打分一致性 + 10 项接口）。

| 周 | 内容 | 状态 |
|---|---|---|
| 1 | 数据层：dump → 候选集 → 灌库 → tag 清洗 | ✅ |
| 2 | P0 推荐 + 选题 + 续作折叠 + API + pgvector + 前端 v0 + 部署 | ✅ |
| 3 | Embedding 建库 ✅ · 问卷选题多样化(MMR) ✅ · P1 融合 staff/studio ✅ | ✅ |
| **4** | **萌娘百科语料 + HyDE + 混合检索** | ⬜ **← 从这里继续** |
| **5** | **离线评测（核心卖点，不可压缩）** | ⬜ |
| 6 | 信息增益选题 + 账号系统 + 季度同步 | ⬜ |

✅ **带前端的部署配置已实测通过（2026-08-15）** —— 此前这里挂着「唯一未经实测的
环节」，现已消除。`buildCommand` + `outputDirectory` + 只转发 `/api/*` 这套组合
线上验证：根路径返回 Vite 构建产物（`/assets/index-*.js` 带 hash），
`/api/health` 返回 `catalog_size=11453 · with_tag_vec=11311 ·
dict_fingerprint=6a1cbbe1bc4f446d`。根目录没有 `package.json` 并未让 Vercel 的
install 阶段报错，**不需要把 Install Command 设成空**。

⬜ **小缺口：`/health` 只报 `with_tag_vec`。** 第 3 周之后打分链路多了 `vec` 和
`staff_vec` 两列，但健康检查看不到它们的非空行数 —— C 节那句「跳过 pytest
就没人发现向量是不是漏跑了，`/health` 也能看出来」现在只对 tag 那一路成立。
加两个字段是几行的事，下次动 `server/main.py` 时顺手补。

## ✅ 第 3 周动作清单（2026-08-14 定，2026-08-15 全部完工）

> 📌 **保留是因为论证过程有用**（为什么否决 k-means、为什么必须存 sparsevec、
> 为什么三路分开算余弦），**不是待办**。第 6 条是唯一剩下的，且标记为可选。
> 下一步看本节末尾的「⬜ 下一步：第 4 周动作清单」。

按依赖顺序。前两步是「越晚做代价越大」的前置动作。

**0. 改列类型 `vector(1024)` → `halfvec(1024)`** —— ✅ **已执行**
省 23.5 MB（47 → 23.5 MB）。⚠️ **必须在灌数据之前跑**：现在列全 NULL，改类型
是一条 ALTER；灌完 11,453 条再改要重灌。
[sql/003_vec_halfvec.sql](sql/003_vec_halfvec.sql)，幂等，已在事务里试跑+回滚验证过。
⚠️ 里面记了一条 parity 纪律：fp16 相对精度约 1e-3，**远大于** `test_parity.py` 的
`SWAP_TOL=1e-4` —— 所以两条打分路径必须都从库里读，不能一条读库一条读缓存。

**1. 摸清 embedding API 的行为** —— ✅ **已跑完，结果见 A.7 末尾的探测结果表**
[scripts/probe_embedding_api.py](scripts/probe_embedding_api.py)，
`uv run --group etl python scripts/probe_embedding_api.py`，约 ¥0.001。
不是「本地 vs API 比对」（那个方案已废弃），测的是唯一那条路径自己的行为：
维度/归一化 · 跨请求确定性 · 批内不变性 · instruct 前缀是否可控 ·
`dimensions` 参数 · 语义合理性。
⚠️ 语义合理性用 `tag_vec` 挑「无关」样本对，**不能按 subject_id 顺序取** ——
实测相邻 id 往往是同系列（`OFFSET 5000` 取到的是奶油柠檬第十三/十四部分），
拿同系列当反例必然假红。

**2. 建 embedding 缓存层**（见 A.7）—— ✅ **已完成**
[src/embed_cache.py](src/embed_cache.py)，SQLite，键 = `hash(MODEL + DIM + text)`，
落在 `data/interim/embed_cache/`（已被现有忽略规则覆盖，不进 git 也不进 Neon）。
⚠️ **它的理由在实测后变了**：原写「是第 4 周迭代 chunk 切分策略的前提」，
但实测 API 一次全量重建只要 ~27 分钟 / ¥1.92，迭代提速不再是主要理由。
真正的理由是**可复现性 —— 而且它是唯一来源**：API 用连续批处理，
同一条文本重发拿不回同一批数字（余弦 0.99987），只有从缓存重放才是精确的。
💡 第 3 周已经兑现过一次价值：写库阶段炸了之后重跑，100% 命中、零成本零耗时。

**3. 编码 summary → `anime_profile.vec`** —— ✅ **已完成（2026-08-14）**

```
10,864 / 11,453 非空（589 部空 summary 存 NULL，对得上）
库 85 MB（VACUUM FULL 前 109 MB）· 耗时 11 分 44 秒 · ¥0.19
build_meta['embed_vec'] 指纹 b27080d522cd9f05 · 20 项测试全绿
```

📌 **立项假设已验证：142 部零 tag 向量的作品，embedding 救回 131 部（92%）**，
只剩 11 部仍无向量。这批（欧美动画 + 国产老动画）此前在 P0 里
**永远无法被 tag 余弦召回**。

📌 **第 8 节记录的失败案例① 有了可复现的前后对照**，第 5 周报告直接可用：

| | 大闹天宫的最近邻 |
|---|---|
| tag 余弦（P0） | 修罗武神 · 长生界 等现代网文改（tag 只有 `玄幻`+`小说改`） |
| **embedding** | **金猴降妖(0.79) · 西游记(0.78) · 人参果(0.73)** —— 全是上美影西游题材 |

其余抽查：千与千寻 → 夏目友人帐/龙猫/崖上的波妞；EVA → 高达00/攻壳；
CLANNAD → 君吻/幸运星。

⚠️ **下面这段是原始计划，保留作对照** ——
⚠️ **喂进去的文本只放 `summary`，不拼 name、不拼 tags —— 理由见 A.10**（会污染第 5 周 ablation）。
⚠️ 589 部 summary 为空（5.1%）→ **存 NULL**（理由见 A.9）。代价是这批在向量检索里
永远召不回，与第 13 节那 142 部零 tag 向量的作品**大概率不是同一批**，
值得先交叉看一下：如果两批基本不重叠，说明 embedding 确实补上了 tag 的缺口，
这本身就是第 5 周报告里的一个论据。

**4. P1：融合 staff/studio 结构化特征** —— ✅ **已完成（2026-08-14）**

```
match = (Σ wᵢ·cosᵢ) / Σ wᵢ            三路各自归一化后加权，再按参与权重归一
rank_score = α·match + (1−α)·quality   现有 blend 结构不变
默认 w_tag=0.3 / w_emb=0.6 / w_staff=0.1（⬜ 占位值，第 5 周要扫）
```

产出：`staff_vec sparsevec(1933)` 10,269 行 + `data/interim/staff_vocab.json`
（[sql/006](sql/006_staff_vec.sql) · [src/staffvec.py](src/staffvec.py) ·
[scripts/build_staff_vectors.py](scripts/build_staff_vectors.py)），
两条打分路径都改（[src/recommend.py](src/recommend.py) 的 `Weights`/`_cosines` +
[src/recommend_sql.py](src/recommend_sql.py) 的动态 match 表达式），
API 暴露 `w_tag/w_emb/w_staff`（**第 5 周跑四条 baseline 的入口**）。

**三个设计决定：**

- **三个空间分开算余弦，不拼成一个大向量。** 拼接等于让 308 维 tag 和
  1024 维 embedding 按维数比例隐式分权重，而我们要的是显式可调、可扫描的权重。
- **μ 只算一次、三路共用。** 各空间各算各的 μ 的话，同一条评分在 tag 空间是
  「喜欢」、在 embedding 空间可能变成「不喜欢」，融合出来没有意义。
- ⚠️ **某一路偏好向量为零时整项跳过，不是当成 0 相似度。** 贡献 0 会对所有
  作品一视同仁地稀释另外两路。而且 **pgvector 对零向量的 `<=>` 返回 NaN**，
  `ORDER BY match DESC` 对 NaN 不报错 —— 又是「不报错但全错」。

⚠️ **存储必须用 `sparsevec`。** 1,933 维但每部只有 4.1 个非零值：
`vector` 要 88 MB、`halfvec` 44 MB、**`sparsevec` 实测 0.47 MB**。
为 99.8% 的零付 44 MB 不可接受，而 sparsevec 同样支持 `<=>`，链路形状不变。

⚠️ **P1 的论证框架变了。** 原文档说它对付「区分信息在库里但没进向量」
（大闹天宫→上美影、攻壳→Production I.G），但 **embedding 已经顺手解决了
大闹天宫那个案例**。所以 P1 现在要证明的是「**在 embedding 之上还能再加多少**」，
不是「比 tag 好多少」—— 第 5 周报告要按这个改。
⚠️ 而乐队番那个案例（孤独摇滚 vs MyGO 的基调差异）staff/studio 解决不了，
只能靠简介 embedding —— **这两类论据必须分开讲**。

**5. 问卷选题多样化** —— ✅ **已完成（2026-08-14），但方案与原计划不同**

原计划是「PCA → k-means → 每簇选代表」。**动手前的三个诊断直接推翻了它**
（这三个诊断本身就是这一步最大的产出，详见第 9 节顶部的标注）：

```
silhouette ≈ 0.035，随 N 单调下降  → 无分离，且定不出 N
ARI 跨种子 ≈ 0.48                  → 一半簇结构是随机的，不可复现
PC1 vs 简介长度 r=+0.053           → ✅ 这项虚惊，PC1 没被长度占据
```

⇒ 改用 **MMR（最大边际相关）**：`argmax[ λ·热度 − (1−λ)·与已选的最大相似度 ]`。
实测 N=30 冗余 **0.4552（纯热度）→ 0.3781**，而中位热度只从 44,146 掉到 41,040；
k-means 代表是 0.4101/38,990，**MMR 两项都更优**。且 MMR 确定性、无随机种子。

产出：`mmr_rank` 4,439 行（[sql/005_mmr_rank.sql](sql/005_mmr_rank.sql)）+
`cluster_id` 30 簇（留作第 5 周 baseline），
由 [scripts/build_clusters.py](scripts/build_clusters.py) 一次算出。
`questionnaire.select_items` 已改为按 `mmr_rank` 排序，测试同步更新
（原来断言「按热度降序」，那正是要打破的）。

⚠️ **第 5 周那条冷启动曲线仍不能只跑一个种子出一个数。** MMR 本身确定，
但对照线里的 k-means 有种子方差（ARI≈0.48），要跑 5–10 个种子报
**均值 ± 标准差**，否则「内容簇比随机好 3%」可能整个落在噪声里。

⬜ **HDBSCAN 结构性对照没做** —— silhouette 和 ARI 已经足够判定「不该用聚类」，
它只会再确认一次。第 5 周若要在报告里论证「空间是连续体」可以补跑。

**5b. 问卷多次作答** —— ✅ **已完成（2026-08-14），后端就绪、前端未接**

用户可反复做问卷，跳过已评分的继续往下取题，答案累积进个人资料库。

```
GET /api/questionnaire?n=30&exclude=1,2,3
select_items(conn, n, *, exclude: Collection[int] = ())
```

💡 **MMR 的结构恰好使这件事成立，这不是设计时预见到的。**
`mmr_rank` 存的是**全池 4,439 条的完整贪心序**，而 MMR 的第 31 位定义上就是
「已选前 30 位的前提下信息增量最大的那一部」—— 所以**第二轮不是随便往下顺延，
它本来就是为补充第一轮之外的信息而选的**。
⚠️ 若当初只存前 30（更直觉的实现），这个功能得重做排序逻辑。
测试里把这条写成了断言：第二轮结果必须**等于一次性取 60 题的后 30 题**。

**两条判据（2026-08-14 Kevin 定）：**

- ⚠️ **排除集 = 有评分记录的，不是「已作答的」。** `to_rating()` 对 `skip`
  返回 None、不产生记录，所以「没看过」天然会在下轮再出现 —— 这是有意的。
  **不需要为此维护第二个集合。**
- ⚠️ **只看条目自己有没有评分，不在系列内传递。** 用户给《JoJo 第三部》打过分
  不代表看过第一部，这类每季可独立观看的作品跳着看很常见。
  问卷的目的是扩充资料库，只要根节点本身没评分就该问。
  （按系列传递会静默少掉大量可问的题。）

⚠️ **`exclude` 保持了第 2 节的架构铁律**：调用方传入，服务端不区分它来自游客的
localStorage 还是注册用户的 `user_rating` 表。**第 6 周加账号只是换数据源，
选题与打分链路一行不用改。**

⚠️ **缓存改了**：原先缓存「前 n 题」，现在缓存**候选序列**（`_POOL_CACHE = 600`），
per-user 的排除在内存里过滤 —— 排除集放进缓存键会让键空间爆炸。
顺带把 `n` 从缓存键里去掉（原先每个 n 各占一个条目）。
600 条 ÷ 30 题 = **能撑 20 轮**，而实测第 6 轮中位热度仍有 18,462。

实测三轮（每轮 8 题）零重复且轮内依然分散：
孤独摇滚/猫和老鼠/Fate/Zero/EVA… → 进击的巨人/芙莉莲/JOJO/四月是你的谎言… →
魔法少女小圆/化物语/命运石之门/紫罗兰永恒花园…

**6.（可选）统计全库 tag 共现矩阵** —— ⬜ **唯一没做的一条，仍然可选**
看 `轻小说改`+`小说改`、`异世界`+`穿越`、`后宫`+`校园` 的 PMI 有多高。
⚠️ **只统计不调权重** —— 改权重会污染第 5 周 baseline 的口径。

---

## ⬜ 下一步：第 4 周动作清单（2026-08-15 定）

**萌娘百科语料 + HyDE + 混合检索。** 这是**请求路径上第一次出现模型调用**
（前三周的流程 A 全程零模型），也是第一次往库里灌十万量级的行 ——
两件事都会撞上此前只在文档里推演过的约束。

### 四个前置动作（都是「越晚做代价越大」）

**0. 把 httpx 挪进主依赖组** —— ✅ **已完成（2026-08-15）**
`.vercelignore` 只排 `scripts/` **不排 `src/`**，所以 [src/embed.py](src/embed.py)
早就随 `src/` 上线了，只是没人 import 它 —— Python 的 import 是惰性的，
不被 import 就不会执行里面的 `import httpx`。**一颗惰性炸弹。**
引爆点在第 4 周 `server/` 引用它去编码查询：那是**模块级 import**，
`api/index.py: from server.main import app` 整条链断掉 → ASGI app 构建不出来
→ `/health` `/questionnaire` `/recommend` **全部 500**。
⚠️ **一个第 4 周的新功能会把第 2 周就上线的推荐一起打死**，
而开发机装了 etl 组，本地和测试全绿 —— 与「连接池不放 lifespan」
「recommend.py 读不入 git 的 series_root.json」同族：本地好好的，上线就挂。

**实测（隔离环境，未动开发机 .venv）**：
```
uv sync --no-dev --no-group api --no-group etl --no-group ml   → 23 个包
  import server.main   ✅ 不回归
  from src import embed ✅ 第 4 周要走的那条 import
反向验证：卸掉 httpx 后 from src import embed
  → ModuleNotFoundError: No module named 'httpx'    ← 故障是真的
```
净增 4 个纯 Python 小包（httpx/httpcore/h11/certifi，几 MB），
anyio/idna/typing-extensions 已随 fastapi→starlette 进来了。

💡 **顺带修掉一个没预料到的隐患**：`fastapi.testclient.TestClient` **自身依赖 httpx**。
改之前若按 D 节那条光跑 `uv sync`（不带 `--group etl`）再跑 pytest，
`test_api.py` 那 10 项会直接挂在 TestClient 导入上。现在 httpx 在主依赖组，
**任何装法都有它**。

**1. 给 embedding 请求加并发** —— ⬜ 8–16 路
第 3 周实测每批（32 条）往返 **2.2 秒且串行**，profile 340 批跑了 11 分 44 秒。
10 万 chunk 是 3,125 批 ≈ **115 分钟**。RPM 2,000（≈33 req/s）离用满还差两个
数量级，**并发是免费的加速**，能压到十几分钟。
⚠️ SQLite 缓存的写入要串行化。

**2. 灌库脚本做成三段式** —— ⬜ 读库 → 长耗时 API → 写库，每段各开各的连接
第 3 周已经踩过：握着 `db.connect()` 跑完 11 分钟的 API 阶段，写库时
`SSL connection has been closed unexpectedly` —— **Neon 是 serverless，
空闲连接会被回收**。chunk 阶段可能跑一两小时，必然再撞上。
[scripts/build_embeddings.py](scripts/build_embeddings.py) 的写法照抄。

**3. 定切分粒度前先算天花板** —— ⬜
⚠️ **维度已定死，唯一还能把存储撑爆的变量是 chunk 条数。**
按每条 ~2,400 字节（halfvec(512) 1,024 B + HNSW 摊销）：可用空间 ≈
450 − 110 − 10 = 330 MB ÷ 2,400 ≈ **13.7 万条天花板，计划 9 万，余量 52%**。
把 chunk 从 400 字改成 200 字，条数直接翻倍 —— **这条红线要在调切分策略时随时对照**。

### 主线三步

**4. 抓萌娘百科**（批次 2：~2,000 部系列条目 → ~3 万 chunk）
MediaWiki API，礼貌速率 1 req/s。
⚠️ **剧透靠现成的 `heimu` CSS class 离线打标，不是运行时让 LLM 判断** ——
第 15 节原则 2，离线更可靠也更省。

**5. 切 chunk → 编码 → 灌 `plot_chunk`**（`halfvec(512)` + HNSW）
⚠️ **分批灌 + 每批后跑普通 `VACUUM`，不要 `VACUUM FULL`。** 它重写整张表、
产生等于表大小的 WAL，而在 Neon 上 **WAL 进存储计量** —— 第 3 周两次
VACUUM FULL 就贡献了约 150 MB。库到 324 MB 时全表重写还可能直接顶穿上限，
**而 Neon 超限是挂起项目不是计费**。
⚠️ 正文**不进 Neon**，只存 `content_ref` 指向 R2/静态 JSON。
⚠️ 查询向量是 1024 维，搜 chunk 时客户端截断到 512 再归一化（MRL 合法，
已实测客户端截断 vs 服务端截断 cos = 1.000000）。缓存**永远存 1024**。

**6. HyDE 查询改写 + BM25/向量混合检索**（流程 B）
BM25 那条腿第 1 周就建好了（`search_tsv` + jieba 预分词，词典指纹已在启动时校验）。
⚠️ **LLM 可以 fallback，embedding 绝对不行**（A.8）—— 换 embedding 模型
不会报错，会返回一个排好序的噪声列表。配额真断了的降级方向是**退回纯 BM25**。
⚠️ 查询词必须走**同一套 jieba + 同一份词典**，否则和库里对不上、召回直接崩。

### 顺带留意

⚠️ **Neon 还有一条独立配额：网络传输 5 GB/月。** 第 3 周一天跑掉 2.68 GB，
靠 `binary=True` + npz 本地缓存压住的。第 4 周离线路径的传输量会更大，
**这条配额的压力全部来自离线路径，线上单次响应才 4.6 KB。**

---


# 第二部分 · 系统怎么运转（操作手册）

> 实操以本部分为准。第五部分的设计稿若与此冲突，以这里为准。

## A. 模型依赖与运行流程（2026-08-14 新增）

> 此前这些信息散在第 2、4、6、7 节里，第一次读的人拼不出全貌。这里是唯一的全景。

### A.1 整个项目只用到两类模型

| # | 类型 | 具体 | 用途 | 能否换 | 状态 |
|---|---|---|---|---|---|
| 1 | **Embedding** | Qwen3-Embedding-0.6B | 建库编码 + 查询编码 | ❌ **锁死** | ⬜ 第 3 周 |
| 2 | **LLM** | 未定 | HyDE 查询改写 | ✅ 可 fallback | ⬜ 第 4 周 |
| 3 | **LLM** | 未定 | 剧情问答生成 | ✅ 可 fallback | ⬜ 第 4 周 |

⚠️ **只有一个 embedding 模型，但它有三个调用点。** 这是最容易搞混的地方：

```
① 离线：11,453 条 summary        → anime_profile.vec      第 3 周
② 离线：3–10 万 chunk            → plot_chunk.vec         第 4 周
③ 线上：用户查询 / HyDE 假想文档  → 临时向量，用完即弃      第 4 周
```

**这三个必须是同一个模型、同一套参数**，因为 ③ 算出来的向量要和 ①② 存在库里的
向量比余弦。这是下面 A.5「什么能 fallback」那条铁律的全部来源。

### A.2 这些**不是**模型（澄清，前几轮讨论反复混淆过）

| 组件 | 实质 | 注意 |
|---|---|---|
| **jieba 分词** | 词典 + 规则 | 无模型，但**同样要锁死**（`BUILD_FINGERPRINT` 指纹校验）。理由与 embedding 完全一致：建库和查询必须是同一个函数 |
| **tag 余弦（P0）** | numpy / pgvector 算术 | 零模型 |
| **BM25** | Postgres tsvector | 零模型 |
| **PCA + k-means** | sklearn 算法 | 不存在「聚类模型」这个东西。聚类是**两级**：embedding 模型产出几何，聚类算法在几何上切分，两级各有独立方差 |
| **续作折叠 / 评分下限 / blend 重排** | SQL + 规则 | 零模型 |

⚠️ **所以现在线上跑的整套推荐（P0）是零模型的。** 这不是巧合 —— 正因为请求路径上
不需要加载任何模型，Vercel serverless 才成立（见 A.6）。

---

### A.3 三条线上流程

#### 流程 A · 推荐 —— ✅ 已上线，**全程无模型调用**

```
用户
 │
 ├─ GET /api/questionnaire ──► 聚类选出的代表作（模块级 dict 缓存，容器内跨请求存活）
 │
 ├─ 作答四选一：看过+打分 / 想尝试 / 不感兴趣 / 没看过
 │   ⚠️ 前端只传 choice 不传分数，分数与置信度由服务端 to_rating() 映射
 │
 └─ POST /api/recommend
      │
      ├─[查库 1]─► 取用户评过的那几十部的 tag_vec
      │            ⚠️ 不能加 tag_vec IS NOT NULL —— 零向量作品对向量和
      │               贡献为 0，但仍要参与 μ 的计算，漏掉会让 μ 偏移
      │
      ├─[本地算]─► μ = (Σc·r + k·prior)/(Σc + k)，prior=7.07，k=2
      │            偏好向量 = Σ (r − μ) × c × tag_vec
      │
      ├─[查库 2]─► pgvector 对 11,311 行暴力算余弦（实测 ≈ 0 ms）
      │            过滤：非 nsfw · 评分 ≥ 3.5 · 年代窗口 · 剔除已看过/不感兴趣
      │            召回 top_k × 10 → 续作折叠 CTE
      │            ⚠️ ORDER BY 必须带次级键 subject_id
      │
      ├─[本地算]─► blend 重排：α·匹配 + (1−α)·贝叶斯加权评分，α=0.5
      │
      └─[查库 3]─► 取理由（命中的 tag）+ 元数据
                   → 返回，线上实测中位 235 ms
```

⚠️ **第 3 周 P1 之后这条流程的形状不变** —— 只是偏好向量从「308 维 tag」
变成「tag + embedding + staff/studio 融合」。embedding 是**预先算好躺在 `vec` 列里的**，
请求时仍然不调任何模型。

#### 流程 B · 自然语言检索 —— ⬜ 第 4 周（**第一次出现请求时的模型调用**）

```
用户输入「有没有主角很强但很低调的番」
 │
 ├─[Redis 缓存 hash(query)]──► 命中则直接跳到检索
 │
 ├─[LLM 调用]──► HyDE：改写成一段**假想的动画简介**
 │               （查询和简介的文体差太远，直接编码查询效果差）
 │
 ├─[Embedding 调用]──► 编码假想简介
 │
 ├─[查库]──┬─► pgvector 搜 anime_profile.vec   （语义腿）
 │         └─► BM25 搜 search_tsv，查询词走同一套 jieba（关键词腿）
 │
 └─ 混合融合 → 结果
```

#### 流程 C · 剧情问答 —— ⬜ 第 4 周之后

```
用户问「XX 最后结局是什么」
 │
 ├─ 先确认是哪部作品 → subject_id（走 alias 表）
 │
 ├─【剧透门控】先只检索非剧透 chunk
 │   ⚠️ 靠萌娘百科现成的 heimu CSS class **离线打标**，不是运行时让 LLM 判断
 │
 ├─[Embedding 调用]──► 编码问题
 │
 ├─[查库]──► plot_chunk 的 halfvec(512) + HNSW
 │           ⚠️ 必须限定在该 subject_id 范围内（实体消歧铁律，见第 15 节原则 4）
 │
 ├─[取正文]──► content_ref 指向 R2/静态 JSON。⚠️ 正文不存 Neon，存不下
 │
 ├─[LLM 调用]──► 生成回答
 │
 └─ 用户显式确认要剧透 → 放开门控重新检索一遍
```

---

### A.4 离线流程（用户看不见，但决定了线上有什么）

```
【第 1 周 · ✅ 完成】Bangumi dump 410 MB
   └─ candidates.py 筛出 11,453 部（口径唯一事实来源）
       ├─ load_profiles.py     → anime_profile + alias + search_tsv
       ├─ backfill_staff.py    → studios / staff（走 dump，非 AniList）
       ├─ backfill_anilist.py  → anilist_id / name_en / popularity
       ├─ build_series_map.py  → series_root
       └─ build_tag_vectors.py → tag_vec(308)
           ⚠️ 三个回填脚本各管各的列，绝不交叉 UPDATE（详见 C 节）

【第 3 周 · ✅ 完成】
   summary ──[Embedding API]──► anime_profile.vec  halfvec(1024)，10,864 条
       └─ 途中落 SQLite 缓存 data/interim/embed_cache/（可复现性的唯一来源）
   vec ──[MMR]──► mmr_rank → 供问卷选题   ⚠️ 不是 PCA→k-means，那个方案被实测否决
       └─[PCA → k-means]──► cluster_id    留作第 5 周冷启动曲线的对照线
   staff/studio ──► staff_vec sparsevec(1933) ──► P1 三路融合

【第 4 周 · ⬜ 下一步】
   萌娘百科 ──抓取──► 切 chunk ──[Embedding]──► plot_chunk
                                    正文 ──► R2

【第 5 周 · ⬜ 核心】
   离线评测：leave-one-out / NDCG@10 / 四条 baseline / 冷启动曲线
   ⚠️ 走 numpy 路径（SQL 往返做不到 10⁵~10⁶ 次打分）
   ⚠️ 与 SQL 路径由 tests/test_parity.py 18 项锁死等价

【第 6 周 · ⬜】
   GitHub Actions 季度增量同步（走 Bangumi API 而非 dump）
   账号系统：user_rating 表替代 localStorage
```

### A.5 总图

```
              ┌──────────── 离线（跑一次，或每季度） ────────────┐
              │  dump / 萌娘百科 → ETL → Embedding → 向量入库    │
              └────────────────────┬────────────────────────────┘
                                   │ 写
                              ┌────▼────┐
                              │  Neon   │  86 MB / 500 MB
                              │Postgres │  向量全部预先算好
                              └────┬────┘
                                   │ 读
   ┌───────────────────────────────┴────────────────────────────┐
   │                    Vercel Serverless                        │
   │  流程 A 推荐  ──► 零模型，纯算术          ✅ 已上线 235 ms   │
   │  流程 B 检索  ──► LLM + Embedding API    ⬜ 第 4 周         │
   │  流程 C 问答  ──► LLM + Embedding API    ⬜ 第 4 周         │
   └─────────────────────────────────────────────────────────────┘
```

---

### A.6 ⚠️ Vercel 上永远不能加载模型

不是「难」，是数量级上不可能。**任何「让函数自己跑模型」的想法都不用再试**：

- **bundle 上限 250 MB（解压后）**。CPU 版 torch 就 200 MB+，加 transformers/tokenizers，
  再加 Qwen3-0.6B 权重本身 fp16 约 1.2 GB —— 超一个数量级。
  换 ONNX + int8 量化能压到 600 MB 左右，**还是超**。
- 没有 GPU。
- **冷启动**：这条已经付过学费 —— 内存 Catalog 每请求重建要拉 2.6 MB / 1.31 s 就被判死刑，
  理由是「低流量反而让冷启动更糟」。600 MB 的模型加载在同一逻辑下是灾难性的。

**推论：所有向量必须离线算好写进库，请求时只做查询和算术。**
这正是流程 A 能零模型跑通的原因，也是第 4 周设计流程 B/C 时的硬约束。

---

### A.7 ✅ 已定：embedding 全程走 API + 本地缓存层（2026-08-14）

**原方案**是「本地建库 + 线上查询走 API + 一致性验证」。改掉的理由：
线上**只能**是 API（A.6），所以两者不一致时唯一可行的收敛方向就是把建库也搬到 API 上 ——
「测出来不一致就都用本地」这个选项根本不存在。既然如此，**直接全程 API，
让库和查询在构造上就是同一个函数**，一致性问题从根上消失。

#### 可行性实测（2026-08-14 查库重算）

```
anime_profile.summary：11,453 部 · 空的 589 部(5.1%)
总计 2,809,700 字 · 均 245 · p50 203 · p90 452 · max 7,225
```

按 Qwen 分词器中文约 0.7 token/字：

| 批次 | 条数 | ≈ token | 何时 |
|---|---|---|---|
| `anime_profile.summary` | 11,453 | **≈ 2.0 M** | 第 3 周 |
| `plot_chunk` 批次 2 | ~3 万 | ≈ 8.4 M | 第 4 周 |
| `plot_chunk` 批次 3（可砍） | ~6 万 | ≈ 17 M | 第 6 周后 |

⚠️ **「10 万次限流请求」这个说法是错的，它把成本夸大了一个量级。**
`/v1/embeddings` 是 OpenAI 兼容接口，**输入接受数组**，一次能塞 32–64 条：

- profile：11,453 / 32 ≈ **358 次请求**
- chunk 全量：10 万 / 32 ≈ **3,125 次请求**

#### ✅ 硅基流动实测条款（2026-08-14 核实，此前是估算）

模型 `qwen/qwen3-embedding-0.6b`：

| 项 | 值 |
|---|---|
| 单价 | **¥0.07 / 百万 token**（¥0.000070 / K） |
| 上下文 | 32 K |
| Rate limit（L0 级） | **RPM 2,000 · TPM 1,000,000** |
| 现有额度 | ¥16 |

**⚠️ 这是「按量计费 + 赠送额度」，不是「免费但限速」。** 所以失败模式是
**额度耗尽后停服**，脚本要检测余额/计费错误并停下，而不是无脑退避重试。

按真实单价重算：

| 批次 | token | 成本 | 耗时（TPM=1M） |
|---|---|---|---|
| profile | 2.0 M | **¥0.14** | ~2 分钟 |
| chunk 批次 2 | 8.4 M | ¥0.59 | ~9 分钟 |
| chunk 批次 3（可砍） | 17 M | ¥1.19 | ~17 分钟 |
| **全量一次** | 27.4 M | **¥1.92** | **~27 分钟** |

📌 **¥16 够跑 8 次全量重建。**

⚠️ **上表的「耗时」只按 TPM 算，实测偏乐观 —— 真正的瓶颈是请求延迟。**
2026-08-14 实测：每批（32 条）往返约 **2.2 秒**，而请求是串行的。所以：

| | 请求数（batch=32） | 按 TPM 估 | **实测（串行 × 2.2s）** |
|---|---|---|---|
| profile | 340 | 2 分钟 | **~12 分钟** |
| chunk 10 万条 | 3,125 | 27 分钟 | **~115 分钟** |

RPM 2,000（≈33 req/s）离用满还差两个数量级，**并发是免费的加速**。
✅ profile 实测 **11 分 44 秒 / 334 批**，与上表吻合。
⬜ **第 4 周 10 万 chunk 之前应该加并发**（8–16 路即可把 115 分钟压到十几分钟），
届时注意 SQLite 缓存的写入要串行化。

#### ⚠️ 长耗时任务不能跨阶段持有 Neon 连接（2026-08-14 踩过）

首次全量跑，API 阶段 11 分 44 秒**全部成功**，写库却炸了：

```
psycopg.OperationalError: consuming input failed: SSL connection has been closed unexpectedly
```

原因是脚本开头 `db.connect()` 之后**握着那个连接跑完了整个 API 阶段** ——
**Neon 是 serverless，空闲连接会被回收。**

⇒ **凡是「读库 → 长耗时外部调用 → 写库」的脚本都要做成三段式，每段各开各的连接。**
第 4 周灌 chunk（可能一两小时）必然会再撞上，[scripts/build_embeddings.py](scripts/build_embeddings.py)
里的写法可以照抄。

💡 **缓存层在这里第一次兑现了价值**：10,864 条向量已全部落盘，
修完重跑是 100% 命中，**零成本零耗时**。要是没有缓存，这 ¥0.19 和 12 分钟就白花了。

⚠️ **顺带一个排查教训：`cmd | tail` 会把退出码换成 `tail` 的。**
后台任务因此报「exit code 0」，而实际是失败的。
**要判断成败必须重定向到文件再看 `$?`，不能走管道。**

💡 32 K 上下文对本项目绰绰有余：最长的 summary 是 7,225 字 ≈ 5,000 token，
不需要处理截断。

#### ⚠️ 迭代成本：此前高估了，修正如下

本节原先写「API 一次重建 1–5 小时、5 轮迭代烧掉整周」，并据此把缓存层
论证成「迭代的前提」。**按实测限额，这个估计过重：**

| | 一次全量重建 | 5 轮迭代 |
|---|---|---|
| 本地（RTX 4050） | 15–30 分钟 | 免费 |
| **API（实测 TPM=1M）** | **~27 分钟** | ~2.3 小时 / **¥10** |

**API 和本地在耗时上基本持平**，成本也在赠送额度内。
所以「迭代成本」不再是反对全程 API 的理由 —— 它反而进一步支持了 A.7 的决定。

#### 缓存层设计（仍然必做，但理由变了）

📌 **实现位置**（✅ 2026-08-14 已落地）：

| 文件 | 职责 |
|---|---|
| [src/embed.py](src/embed.py) | **模型的唯一定义处**：锁死的 `MODEL`/`DIM`/`QUERY_INSTRUCT`、指纹、`embed_documents` / `embed_query`、错误分类与退避 |
| [src/embed_cache.py](src/embed_cache.py) | SQLite 缓存，键 = `hash(MODEL + DIM + text)` |
| [scripts/build_embeddings.py](scripts/build_embeddings.py) | 编排：查缓存 → 只请求未命中 → 按 token 节流 → 写 `vec` |

⚠️ **`src/embed.py` 在 `src/` 不在 `scripts/`**，因为它有两个调用方：
离线建库和第 4 周的线上查询编码，而 `.vercelignore` 排掉了 `scripts/`。
✅ **httpx 已挪进主依赖组（2026-08-15）**，所以 `server/` 可以放心 import
本模块。原先它只在 `etl` 组，而 `.vercelignore` 不排 `src/` ——
文件早已上线却没人 import，是颗惰性炸弹。详见第 4 周动作清单第 0 条。

⚠️ **缓存层的论据从「迭代速度」收缩到「可复现性」。** 后者没有被上面的数字削弱
分毫 —— ¥16 额度和宽松限额都保护不了你免受「厂商在同一个模型名下换模型/
量化/服务框架」的影响，而第 5 周评测的可复现性是项目的核心卖点。
迭代提速现在是顺带的好处，不是主要理由。

把 API 返回的原始向量按 `hash(text + model + dim)` **存成本地文件**
（npy/parquet，**不进 Neon**）。这是 Phase 2 文档里 `ingest_raw` 的同一套思路：
先落原始响应，解析和写主表作为独立阶段。

三个收益：

1. **可复现性** —— 重建库 = 从缓存回放，bit-identical。这直接补上了全程 API
   最大的漏洞：厂商在同一个模型名下换模型/量化/服务框架，你收不到通知，
   半年后重跑第 5 周评测拿到不同数字却无法归因。
2. **迭代时只有真正变了的 chunk 需要重新请求**
3. **换解析/归一化逻辑不用重新请求**

代价：本地磁盘几百 MB + 二十行代码。

#### ✅ 探测结果（2026-08-14 实测，[scripts/probe_embedding_api.py](scripts/probe_embedding_api.py)）

| 项 | 结果 | 下游影响 |
|---|---|---|
| 维度 | **1024** | 与 sql/003 的 halfvec(1024) 一致 |
| L2 范数 | **1.000000** | 已归一化 → pgvector 可用 `<#>` 内积算子（比 `<=>` 略快） |
| 跨请求确定性 | cos **0.99987**，非 bit-identical | 见下 ⚠️ |
| 批内不变性 | cos **0.99994** | 同上，是同一现象 |
| instruct 前缀 | cos(无前缀, 加前缀) = **0.797** | ✅ 前缀确实改变向量 → query/doc 非对称编码**可控** |
| `dimensions` 参数 | 支持；客户端截断 vs 服务端截断 cos = **1.000000** | ✅ 客户端截断是纯 MRL，chunk 的 512 维走客户端，缓存存 1024 |
| 语义合理性 | 同系列 **0.551** / 无关 **0.339** | ✅ 模型在中文动画语料上正常 |
| **信噪比** | 信号 0.212 / 噪声 6.4e-5 = **3341×** | ✅ 噪声对检索质量无实质影响 |

#### ⚠️ 实测结论：bit-identical 做不到，而且规避不了

**API 不是逐请求确定的** —— 同一条文本发两次，逐位差在 0 ~ 2e-3 之间跳。
原因是服务端用**连续批处理（continuous batching）**：你的请求会和
**其他用户同时在飞的请求**拼成一个 batch，padding 长度与浮点规约顺序
因此完全不受你控制 —— **把自己的 batch size 定死也没用。**

⚠️ 所以「跨请求确定性」和「批内不变性」测的是**同一个现象**，
不是两件事。第一次探测时前者恰好返回 0.000e+00，那是碰巧不是确定。

⚠️ **判据必须是余弦，不能是逐位差。** 逐位差回答「是否 bit-identical」，
而检索质量只取决于余弦。更进一步，余弦的绝对阈值（0.9999 之类）也是拍脑袋 ——
**要和语料里真实的信号强度比**：这批数据的信号（同系列 − 无关）是 0.212，
噪声 6.4e-5，**信噪比 3341×**。质量上完全不构成问题。

📌 **但这把缓存层从「稳妥起见」升级成了「唯一可行的可复现手段」。**
A.7 原先把缓存论证成迭代提速的工具，实测下来它是**可复现性的唯一来源** ——
重发请求永远拿不回同一批数字，只有从缓存重放才是精确的。

💡 **顺带验证了 halfvec 的选择**：API 自身的噪声（逐位 ~2e-3）比
fp16 的量化误差（归一化向量典型元素 0.031 × fp16 相对精度 ≈ 1.5e-5）
**大约两个数量级**。存 fp32 等于用高精度保存噪声。

⚠️ 顺带纠正第 6 节那句「低于 0.999 说明有**归一化差异**」—— 那个归因是错的。
**余弦对缩放不变**：API 若只是没做 L2 归一化，返回 `k × 本地向量`，两者余弦
**恰好等于 1.0**，测试反而全绿。真正会让余弦掉下来的是**池化方式**
（Qwen3 用 last-token pooling，若 API 实现成 mean pooling 就是完全不同的向量）、
**instruct 前缀**、**模型版本被悄悄替换**。

（归一化本身不是不重要，它影响的是别处：pgvector 用 `<#>` 内积算子时，
以及偏好向量那套加权求和 —— 范数不齐会让权重被悄悄扭曲。但那是建库内部的
一致性，同一条路径出来的向量天然一致。）

⚠️ **Matryoshka 截断不受影响。** MRL 的截断就是「取前 N 维 + 重新归一化」，
客户端自己做完全合法。所以 `halfvec(512)` 和第 10 节那个「512 vs 1024 ablation」
照做不误，不依赖 API 是否暴露 `dimensions` 参数。

---

### A.8 ⚠️ 铁律：什么能 fallback，什么不能

**免费额度用完时自动切换到另一个厂商/模型 —— 这个想法对 embedding 不成立。
不是「效果会变差」，是这个操作在数学上没有定义。**

```
库里 11,453 条向量  ← Qwen3 的空间
配额用完，切到 BGE
用户查询向量        ← BGE 的空间
        ↓
   算余弦 → 数字算得出来，在 [-1,1] 里，结果照常排序返回
```

⚠️ **它不会报错。** 你会拿到一个排好序的推荐列表，看起来完全正常，
实际上是噪声 —— 静默、且伪装成正常输出，是最坏的一类故障。

（维度不同反而安全：`vector(1024)` 塞不进 768 维会直接报错。
真正危险的是**维度碰巧一样的两个模型**，比如 Qwen3-0.6B 和 BGE-M3 都是 1024。）

**判据：看这个调用的输出是不是「相对于某个语料库的坐标」。**

| 调用 | 输出性质 | 能否 fallback |
|---|---|---|
| HyDE 查询改写（LLM） | 一段文本，用完即弃 | ✅ 可以 |
| 剧情问答生成（LLM） | 一段文本，用完即弃 | ✅ 可以 |
| **embedding** | 必须和库里那批坐标同一套基 | ❌ **不可能** |

💡 **好消息是真正烧配额的是 LLM 不是 embedding。** 剧情问答每次要生成几百 token 的
回答，成本比编码一个查询高一两个数量级。所以「多模型 fallback 省钱」这个思路
**放在 LLM 那一侧完全成立**，只是不能放在 embedding 上。

#### ⚠️ 而且配额风险的对象搞错了：线上那点量根本用不完

```
用户查询 ~30 token + HyDE 生成的假想文档 ~200 token ≈ 230 token/次
```

| 场景 | token |
|---|---|
| 1,000 次检索 | 23 万 |
| 10,000 次检索 | 230 万 |
| **离线建库一次（profile + 批次 2）** | **1,040 万** |

**跑一次离线建库 ≈ 45,000 次线上检索。** 作品集项目一年也到不了这个量。
按实测 ¥0.07/百万算，10,000 次线上检索的 embedding 成本是 **¥0.16**。
为这个建一套多模型切换系统，工程量远超收益，何况它还不 work。

✅ **已核实（2026-08-14）：是「按量计费 + ¥16 赠送额度」，不是免费限速。**
所以失败模式是**额度耗尽停服**，脚本必须检测计费类错误并**停下报警**，
不能当成限流去无脑退避重试 —— 那会在额度耗尽后空转。
限速另有其事（RPM 2,000 / TPM 1,000,000），那个才用退避处理。详见 A.7。

#### 真正可行的降级：换检索方法，不是换模型

配额真断了，正确的兜底是**退回 BM25 纯文本检索** —— 第 1 周就建好了
（`search_tsv` + jieba 预分词）：

```
embedding 可用 → BM25 + 向量混合检索（第 4 周的正常形态）
embedding 挂了 → 退化成纯 BM25
```

结果会变差，但**是一致的、可解释的变差，不是噪声**。而且混合检索本来就有
BM25 这条腿，等于免费拿到了降级路径。

#### 双库方案为什么不行

理论上可以存两套向量（`vec_qwen` + `vec_bge`），查询和检索一起切。但：

- profile 层：+23.5 MB（halfvec），勉强放得下
- **plot_chunk 层：向量 102 MB + HNSW 索引 140 MB，翻倍就是 +242 MB —— 直接炸穿 500 MB**

而 Neon 免费层超限是**挂起项目**不是计费。所以双库在 chunk 那一层不可能。

#### 落到纪律上

1. **embedding 锁死一个模型**，写进配置并做指纹校验 —— 与 jieba 词典的
   `BUILD_FINGERPRINT` 同一条纪律，理由也一样
2. **LLM 那一侧可以做 fallback 链**，那里才是配额大户，换模型只影响质量不影响正确性
3. **降级方向是 BM25，不是换 embedding 模型**
4. ⚠️ **第 5 周评测跑的时候 LLM 也要锁死**，fallback 只允许出现在线上演示路径，
   否则评测数字不可复现

---

### A.9 向量存哪（2026-08-14 定）

「embedding 向量」下面是**三个不同的东西**，去处不一样：

| 东西 | 去处 | 大小 |
|---|---|---|
| ① profile 向量（11,453 条） | **Neon** `anime_profile.vec` `halfvec(1024)` | 23.5 MB |
| ② chunk 向量（~10 万条） | **Neon** `plot_chunk.vec` `halfvec(512)` + HNSW | 102 + 140 MB |
| ③ API 返回的原始向量缓存 | **本地磁盘，绝不进 Neon** | ~450 MB |

**① 为什么必须在库里而不是文件里**：线上推荐是 pgvector 在库内算余弦。
这正是放弃 Render 的整个理由 —— serverless 没有常驻内存，向量必须待在查询发生的地方。

⚠️ **589 部空 summary 存 NULL，不要存零向量。** 与 `tag_vec` 保持一致
（理由见 [sql/002_tag_vec.sql](sql/002_tag_vec.sql)）：存 NULL 才能用
`vec IS NOT NULL` 一句话过滤。而且零向量与负偏好向量的余弦是 0，会**高于**
所有负相关作品 —— 就是当初 Top5 冒出虫虫危机的那个坑。

**③ 缓存放 `data/interim/embed_cache/`。** ✅ 已确认这个路径**被现有忽略规则覆盖**，
不用改任何配置 —— `.gitignore` 和 `.vercelignore` 都是 `data/interim/*` +
`!data/interim/tag_vocab.json` 的写法，新目录自动落进忽略范围。

⚠️ **缓存要存 API 返回的完整维度（fp32 原样），不要存截断后的。**
第 10 节那个「512 vs 1024 ablation」要两个维度都能拿到，而 MRL 截断是客户端做的 ——
只缓存 512 的话，想要 1024 就得重新请求，缓存层就白建了。

#### 唯一事实来源是数据库

```
文本 →[API]→ 缓存文件 →[建库脚本]→ Postgres 列 →┬→ 线上 SQL 打分
                                                └→ 第 5 周 numpy 评测
```

**缓存只是建库的输入，永远不在打分路径上被读。** 这是 `build_catalog()` 改成
**读** `tag_vec` 列而不是重算的同一条纪律。

⚠️ **一个很隐蔽的陷阱**：缓存是 fp32，库里是 halfvec（fp16）。如果第 5 周的
numpy 评测图省事直接读缓存文件，它拿到 fp32 而线上 SQL 拿到 fp16，
**`test_parity.py` 会开始飘** —— 而且飘得很小，容易被当成浮点误差放过去。
**numpy 那条路必须也从库里读。**

✅ 已实测这条不变式成立：抽查 300 部，**库内 halfvec 与缓存 fp32 的余弦
最低 0.99999988**，写库路径无损。

#### ⚠️ 读回来的类型：`vector` 和 `halfvec` 行为不同（P1 接入时会踩）

```
tag_vec (vector)  → pgvector.Vector      对象
vec     (halfvec) → pgvector.HalfVector  对象
```

⚠️ **两者都不是 numpy 数组**，`np.asarray()` 直接作用在它们身上得到的是
`dtype=object, shape=()` —— 不报错，但后面的矩阵运算全错。
⚠️ **而且 `HalfVector.to_numpy()` 返回的是 float16**，直接拿去做累加会掉精度。

正确写法就是 [src/recommend.py](src/recommend.py) 里现成的那句：

```python
v = vec.to_numpy() if hasattr(vec, "to_numpy") else np.asarray(vec)
mat[i] = v.astype(np.float32)          # ⚠️ .astype 不能省
```

P1 把 `vec` 接进 `build_catalog()` 时照抄这个模式。

#### ⚠️ P1 融合必须分开存两列，不能预融合

P1 要把 tag（308 维）+ embedding（1024 维）+ staff/studio 融合成偏好信号。

| 方案 | 后果 |
|---|---|
| 预融合成一列 | 省一次距离计算，但**权重被烧死在数据里** |
| **分开存**（✅ 采用） | `tag_vec` 与 `vec` 各一列，查询时算 `α·cos(tag) + β·cos(emb) + …` |

**理由：第 10 节的四条 baseline 里，「tag 模型」和「embedding 模型」是两条
独立的对照线。** 预融合之后没法单独跑纯 tag 或纯 embedding，那两条线**直接做不了**。
而且融合权重本身就是第 5 周要调的东西，烧进数据里意味着每调一次要重写 11,453 行。

代价可以忽略：308 维全库暴力余弦实测 ≈ 0 ms，1024 维约 3 倍算力，仍在噪声里。

⚠️ **同一条理由约束了「喂给 embedding 的文本是什么」—— 见 A.10。**

### A.10 ⚠️ 喂给 embedding 的文本只放 `summary`

不要拼 `name`，不要拼 `tags`。两个理由都不是审美问题，是会污染第 5 周评测的：

- **拼 tags** → 「embedding 模型」这条 baseline 就秘密变成了 tag+embedding 混合体，
  与「tag 模型」那条线不再独立，ablation 失去意义。tag 信号已经在 `tag_vec` 列里，
  A.9 刚决定两者分开存，正是为了让它们可以被单独评测。
- **拼 name** → 作品名会把系列身份泄漏进向量，让同系列相似度虚高。
  这与第 13 节剔除 IP 类 tag 的理由完全一致（「作品名不是口味特征，
  且让同系列相似度虚高」），也会与续作折叠的设计意图打架。

⚠️ 若将来要试「summary + 别的字段」，那是**第 5 周的一个 ablation 变量**，
应该新开一列或新开一次实验，不要直接改这一列的定义 —— 改了就没有对照了。

---

## B. 部署架构：Vercel serverless

> 2026-08-12 从 Render 改道，**推翻第 2 节**的后端选型。

**Kevin 决定放弃 Render，前后端都上 Vercel。** 第 2 节的 Render 选型与
「Hobby 工作区 + Starter 实例 $7/月」那整段作废，但**理由本身仍然成立** ——
只是解法从「买一个不死的进程」换成了「让进程不再需要活着」。

**做不到的那条路（别再试）**：保留内存 Catalog 直接扔上 serverless。
实测每请求重建要拉 **2.6 MB / 1.31 s**，而打分本身只要 12 ms。
⚠️ 更反直觉的是**低流量会让冷启动更糟**：作品集项目访问零星 → 实例频繁回收
→ 大部分请求都是冷启动，而不是偶尔。能靠热实例摊薄冷启动的是高流量服务。

**采用的路**：把余弦推进 Postgres。每请求只拉用户评过的那几十部（70 ms，
几乎全是 RTT），11,311 行的暴力余弦由 pgvector 算 —— 实测**成本 ≈ 0 ms**，
再次印证第 4 节「不建 HNSW」的判断。

新增 `anime_profile.tag_vec vector(308)`（+14 MB，库 44 → 58 MB）与
`series_root integer`，见 [sql/002_tag_vec.sql](sql/002_tag_vec.sql)。

#### ⚠️ P1 之后 parity 有三档容差，各有实测依据（2026-08-14）

| 常数 | 值 | 管什么 | 实测上界 |
|---|---|---|---|
| `TOL` | 1e-5 | fp32 路径（tag/staff）的 match | tag 9.9e-08 · staff 6.2e-08 |
| `EMB_TOL` | 5e-4 | embedding 参与时的 match | **7.73e-05** |
| `RANK_TOL` | 5e-3 | rank_score（min-max 会放大 ~11×） | **8.46e-04** |

⚠️ **偏差全部来自 `vec` 的 halfvec（fp16）存储**，两侧读同一批值、只是累加不同；
**所有情况下结果顺序都完全一致**。放宽的只是数值比较，
「顺序是否相同」由 id 序列检查独立把关 —— 逻辑错误会让顺序整个变掉，拦得住。

⚠️ **决定性理由**：embedding API 自身的不确定性在余弦上就有 ~6.4e-5（A.7 实测），
要求两条路径对齐到比数据源自身可复现性更高的精度，没有意义。

⚠️ **这些数是按 120 组随机档案的实测上界定的。** 首次只测 12 组得到 1.94e-05、
据此定 1e-4，扩样后真实上界 7.73e-05 —— 余量只剩 1.3 倍，换批档案就会假红。
**改这几个常数前先重跑那个测量，不要凭感觉调。**

⚠️ 顺带修掉一处自己引入的精度损失：SQL 侧把偏好向量格式化成字面量时用了
`%.7g`，而 float32 需要 **9 位**才能无损往返（实测 `.7g` 每元素差 1.49e-08）。
改成 `.9g` 后 fp32 路径的偏差降了一半。

#### ⚠️ 两套打分实现，靠一致性测试锁住

线上走 SQL（serverless 无常驻内存），第 5 周评测走 numpy（leave-one-out 要跑
10⁵~10⁶ 次打分，SQL 往返做不到）。这直接顶到第 2 节铁律「评测时不能出现
两套口径」，所以用**两条构造上的保证**顶住，而不是靠纪律：

1. **向量的唯一定义处是 `anime_profile.tag_vec`。** [src/tagvec.py](src/tagvec.py) 算、
   [scripts/build_tag_vectors.py](scripts/build_tag_vectors.py) 写，
   `build_catalog()` 改成**读**这一列而不是重算 —— 两条路径读的是同一批数字。
   （实测库里的向量与内存里逐位差 0.00e+00。）
2. **[tests/test_parity.py](tests/test_parity.py) 逐条比对输出**，13 项覆盖
   3 种 rank_by × 6 种 mode × 11 组开关组合 + 偏好向量 + 边界。CI 里必须绿。

⚠️ **光靠第 1 条不够**：它只保证输入相同，不保证过滤/召回/折叠/重排四步的
语义相同。测试第一次跑就抓出一个肉眼绝对发现不了的 bug ——

> 大量作品与偏好向量**完全正交**（余弦精确为 0），召回池整片并列。
> numpy 侧是「match 降序 + subject_id 升序」（stable argsort + ids 已排序），
> 而 SQL 的 `pool` 层 `ORDER BY match DESC LIMIT` **少了次级排序键**，
> Postgres 对并列给任意顺序 → **两条路径召回的根本不是同一批候选**，
> 但两边结果都「像模像样」。

⚠️ 同时修掉 numpy 侧 `np.argsort(-sims[idx])` 的**不稳定排序**（默认 quicksort）。
它有两个后果：结果不可复现（第 5 周评测要求可复现），以及无法与 SQL 对齐。
改成 `kind="stable"`。

⚠️ **2026-08-14 又抓到一处同类的**：`recommend.py` 的续作折叠读的是
`data/interim/series_root.json`，而**那个文件不入 git**（`data/interim/*` 只放行
`tag_vocab.json`）。在任何新 clone 上 `series.load(required=False)` 返回空映射 →
**numpy 侧静默停止折叠、SQL 侧照常折叠，两条路径就此分叉**。
实测把文件移走后 `test_parity.py` 立刻失败。
questionnaire 早已因同样理由改成读库列（commit 0c16ba4），`recommend.py` 是漏网的。
✅ 已改成读 `Catalog.series_root`（来自库列），现在文件缺失时 parity 依然全绿。
📌 **教训：凡是「不入 git 的文件」参与打分链路，就是一颗定时炸弹** ——
它在开发机上永远正常，只在别人的机器上错，而且是静默地错。

⚠️ 另一处最难发现的不等价：**取被评作品向量时不能加 `tag_vec IS NOT NULL`。**
那 142 部零向量作品对向量和贡献为零，却仍要参与 μ 的计算 —— 漏掉会让 μ 偏移，
进而改变**所有**作品的权重符号，而结果依然像模像样。已单独测。

#### 目录结构与部署

⚠️ **`api/` 目录下的每个 `.py` 都会被 Vercel 当成一个独立的 serverless function。**
所以应用包叫 **`server/`**，`api/` 下只有一个 [api/index.py](api/index.py) 入口，
[vercel.json](vercel.json) 用 rewrites 把所有路径打过去。
把 `schemas.py` 放进 `api/` 会导致构建失败（那个文件没有 handler）。

⚠️ **Vercel 装的是 `pyproject.toml` 的主依赖组，`requirements.txt` 会被完全忽略。**
（2026-08-12 首次部署实测纠正 —— 此前这里写反了，说的是「只认 requirements.txt」。）
它的 Python runtime 检测到 `pyproject.toml` + `uv.lock` 就用 uv 装**主依赖组**。
第一次部署因此装了 polars/bgm-tv-wiki/httpx/tqdm，却**没装 fastapi**，
报 `ModuleNotFoundError: No module named 'fastapi'`。

**所以主依赖组 = 部署清单。** 已按这条重排 `pyproject.toml`：

| 组 | 内容 | 上线 |
|---|---|---|
| **主依赖** | fastapi / psycopg[binary] / psycopg-pool / pgvector / numpy / jieba / dotenv / orjson / **httpx** | ✅ **23 个包** |
| `etl` | bgm-tv-wiki / tqdm | ❌ `scripts/` 专用 |
| `api` | uvicorn / argon2 / pyjwt | ❌ 本地跑服务器 + 第 6 周认证 |
| `ml` | scikit-learn | ❌ 第 3 周聚类 + 第 5 周评测 |

⚠️ 顺带查出 **polars 全仓库零 import**（git 历史里也没用过），已整个删除 ——
当初按「400 MB jsonlines 用 polars 更快」加的，但实际 ETL 全是 orjson 逐行解析，
流式读 jsonlines 本来就不需要 DataFrame。它带 55 MB 的 `polars-runtime-32`。
⚠️ **`uv sync` 不再自带 ETL 依赖** —— 跑 `scripts/` 要加 `--group etl`。
⚠️ `requirements.txt` **已删除**：它被忽略却看着像事实来源，是纯粹的漂移隐患。
校验方式：`uv sync --no-dev --no-group api --no-group etl --no-group ml`
后跑 `uv run --no-sync python -c "import server.main"`，这精确复现了线上的安装集合。

⚠️ `data/raw` 是 **2.1 GB**。`.gitignore` 在走 git 部署时挡得住，但
`vercel deploy` 从本地上传时**不看 .gitignore**，靠 [.vercelignore](.vercelignore) 再挡一次。
但 `data/interim/tag_vocab.json` 是**运行时依赖**（jieba 词典 + keep_tags），不能忽略。

```bash
uv sync --group api
uv run uvicorn server.main:app --reload     # 本地，不走 api/index.py
uv run pytest tests/ -q                     # 改过任一条打分路径后必跑
```

#### ✅ 部署已验证（2026-08-12）

`animetion-recommender.vercel.app`，四个接口全部实测通过。构建日志确认
`Using Python 3.12 from pyproject.toml` / `Installing required dependencies from uv.lock`。

**线上实测延迟（从国内打，含跨国到边缘的 RTT）：**

| 接口 | 中位 |
|---|---|
| `GET /health`（1 次查库） | 204 ms |
| `GET /questionnaire`（缓存后） | 166 ms |
| `GET /search`（BM25） | 205 ms |
| `POST /recommend`（3 次查库） | **235 ms** |

⚠️ **那 ~200 ms 基线是「国内 → Vercel 边缘」的 RTT，不是应用成本。**
有意义的是差值：**`/recommend` 只比 `/health` 多 31 ms**，
而它多了两次往返 + 整个续作折叠 CTE。函数与 Neon 同区（iad1 ↔ us-east-2）后
DB 往返已经便宜到可以忽略。

⚠️ **此前「扣掉 RTT 后 ~110 ms 几乎全是折叠 CTE」的归因是错的。**
开发机上那 110 ms 里，大部分是把 200 行召回池**跨国传回来**的时间，
不是查询执行时间。同区实测（2 次额外往返 + CTE）总共只有 31 ms ——
「优化那个 CTE」的优先级远低于原先记录。
教训：**跨国链路上测出来的「查询成本」包含传输量，不能直接当执行成本用。**

原先估算的 ~155 ms 偏保守，实际应用侧开销更小。

⚠️ **`idx_alias_trgm` 确认不用重建** —— 开发机上 trgm 全扫扣掉 RTT 约 90 ms
（同样含传输，真实执行更低），与第 1 周「3.8 万行顺扫几十毫秒，兜底够用」吻合。

**Vercel 配置的三处坑**（都实测踩过，详见 [api/index.py](api/index.py) 的 docstring）：
`vercel.json` 不接受注释键（`"//"` 会被 schema 拒掉）· `memory` 在 Active CPU
计费下被忽略且会刷警告 · `api/` 下每个 `.py` 都会变成独立 function。

**几个已定的接口决策：**

- **`score()` 改回结构体 `Recommendation`**（原三元组 + 「顺序不按第三项排」的隐式约定）。
  现在 `{match, quality, rank_score}` 三个字段各自独立，**列表恒按 `rank_score` 降序**，
  这条不变式对三种 `rank_by` 都成立。⚠️ `rank_score` 的量纲随 `rank_by` 变
  （match→[-1,1]，quality→[0,10]，blend→[0,1]），**不要跨请求比较或展示给用户**。
- **前端传 `choice` 不传分数。** 请求体是 `{subject_id, choice, score?}`，
  分数/置信度由服务端的 `to_rating()` 映射。⚠️ 让前端算等于把映射复制进
  TypeScript，一漂移就是静默的推荐质量下降 —— 与「三种作答带不同置信度」是同一条纪律。
- **端点一律写 `def` 不写 `async def`。** psycopg 同步、numpy 占 GIL，
  写成 async 会阻塞事件循环把并发拖成串行；同步端点由 FastAPI 丢线程池才对。
- **连接池惰性初始化，不放 `lifespan`。**
  ⚠️ serverless 平台不保证执行 ASGI lifespan 事件，放那里线上会拿到一个
  None 池，而本地 uvicorn 一切正常 —— 典型的「本地好好的，上线就挂」。
  分词指纹校验一并挪进去。问卷结果仍用模块级 dict 缓存（容器内跨请求存活）。
- **连接池走 `DATABASE_URL`（pooler），未配则退回直连**（本地开发方便）。
  ⚠️ 必须带 `prepare_threshold=None` —— Neon 的 pooler 是 PgBouncer transaction 模式。
  ⚠️ 必须带 `configure=db.prepare` 注册 pgvector 适配器，否则 `tag_vec`
  **读回来是字符串**、numpy 数组也没法当查询参数，而且不报类型错，
  是在后面某处解析失败。
- **所有路由挂在 `/api` 下**（含 `/api/docs`）。这是同源部署的前提 ——
  `vercel.json` 靠前缀把 `/api/*` 转给函数、其余交给 `web/dist`。
- **CORS 只服务本地开发**（Vite 5173 → uvicorn 8000）。
  ⚠️ **线上同源，这段中间件不参与，`CORS_ORIGINS` 线上不要配。**
  若线上报跨域，说明前端把请求打到了别的域名 —— 该查 `web/src/api.ts` 的
  `BASE`（应恒为相对路径 `'/api'`），不是改这里。
  ⚠️ 仍不写 `["*"]`：第 6 周若把 JWT 放 httpOnly cookie，`"*"` 与
  `allow_credentials` 互斥。

~~⬜ 遗留：续作折叠的三层 CTE 是 `/recommend` 的主要成本~~
**已作废（2026-08-12 线上实测）**：同区部署后「2 次额外往返 + 整个 CTE」
总共只有 31 ms。原先那个 ~110 ms 是跨国传输 200 行召回池的时间被误算成了
执行时间。CTE 不构成瓶颈，别为它增加复杂度。

⬜ **遗留：`/recommend` 是三次往返**（取向量 → 打分 → 取理由 + 元数据）。
取理由那次可以并进主查询（把 `tag_vec` 一起 SELECT 出来），代价是召回池
200 行 × 1.2 KB = 240 KB 传输。同区 RTT 只有 15 ms 时不划算，先不动。

## C. 脚本职责与启动顺序

⚠️ **三个脚本各管各的列，绝不交叉 UPDATE** —— 否则谁后跑谁赢，是最难查的 bug。

| 脚本 | 负责的列 |
|---|---|
| [scripts/load_profiles.py](scripts/load_profiles.py) | 全部 dump 派生列 + `search_tsv` + `alias` 表 |
| [scripts/backfill_staff.py](scripts/backfill_staff.py) | `studios` / `staff` |
| [scripts/backfill_anilist.py](scripts/backfill_anilist.py) | `anilist_id` / `name_en` / `popularity` / `external_ids` |
| [scripts/build_id_map.py](scripts/build_id_map.py) | 产出 `data/interim/id_map.json`（4b 的前置依赖，**不入 git，换机器必须先跑**） |
| [scripts/build_series_map.py](scripts/build_series_map.py) | 产出 `data/interim/series_root.json`（问卷折叠的前置依赖，同样不入 git） |
| [scripts/build_tag_vectors.py](scripts/build_tag_vectors.py) | `tag_vec` / `series_root` 两列。⚠️ 改过词表/tag_rules/tagvec 后**必须重跑**，否则打分读到旧向量 |

前三者都幂等，可任意顺序重跑。已实测：重跑不改行数、不洗掉别的脚本填的列。

⚠️ **换机器 / 重新 clone 后的启动顺序**（`data/interim/*` 除 tag 词表外都不入 git）：

```
uv sync --group etl          # ⚠️ 必须带 --group etl：脚本要用 tqdm/bgm-tv-wiki
                             #    （httpx 在主依赖组，任何装法都有）
psql < sql/001_init.sql
psql < sql/002_tag_vec.sql                   # ⚠️ 别漏：没有 tag_vec 列，最后一步会直接报错
psql < sql/003_vec_halfvec.sql               # vec: vector(1024) → halfvec(1024)。幂等
psql < sql/004_build_meta.sql                # ⚠️ 别漏：build_embeddings.py 会前置检查它
psql < sql/005_mmr_rank.sql                  # 问卷选题的多样性排序列
psql < sql/006_staff_vec.sql                 # P1 的 staff/studio 向量列
uv run python scripts/build_id_map.py        # 需要联网，会下 bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # 需要联网，约 125 次请求
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py   # 依赖上一步；打分链路的前置
uv run python scripts/build_embeddings.py    # ⚠️ 需要 .env 的 SILICONFLOW_API_KEY
                                             #    约 12 分钟 / ¥0.19（缓存命中则秒完成）
uv run python scripts/build_staff_vectors.py # staff_vec + data/interim/staff_vocab.json
uv run --group ml python scripts/build_clusters.py   # mmr_rank + cluster_id，依赖 vec
psql -c 'VACUUM FULL anime_profile'          # 回收批量 UPDATE 的 MVCC 膨胀
uv run pytest tests/ -q                      # 验收：28 项测试应全绿
```

⚠️ **`SILICONFLOW_API_KEY` 是新机器唯一需要人工去申请的东西**（见 .env.example）。
没有它 `build_embeddings.py` 会立刻报错退出 —— 前置检查在花钱之前。

💡 **`data/interim/embed_cache/` 不入 git**（50 MB），所以换机器要重新花 ¥0.19 请求一次。
⚠️ 但**如果你手上有旧机器的缓存文件，拷过去就是零成本重建，且 bit-identical** ——
这是全程 API 方案下唯一能保证「两台机器建出同一个库」的办法。

⚠️ **最后两步不是可选的。** 跳过 VACUUM 会让库虚涨一倍（实测 58 → 116 MB），
误判预算吃紧；跳过 pytest 就没人发现 `tag_vec` 是不是漏跑或跑歪了 ——
`build_tag_vectors.py` 没跑的话打分**静默返回空列表**，不报错。
（`GET /health` 的 `with_tag_vec` 字段也能看出来。）

## D. 环境备忘

```bash
uv sync                                       # 只装运行时依赖（= 线上那一组）
uv sync --group etl                           # scripts/ 要用：tqdm/bgm-tv-wiki
uv sync --group api                           # 本地跑 uvicorn
uv run ruff check src/ scripts/ server/ tests/
uv run pytest tests/ -q                       # 改过任一条打分路径后必跑
uv run uvicorn server.main:app --reload       # 起 API，文档在 /api/docs
cd web && npm install && npm run dev          # 起前端（5173），/api 自动代理到 8000
```

中文输出要设 `PYTHONIOENCODING`，**两种 shell 语法不同**：

```bash
PYTHONIOENCODING=utf-8 uv run python ...            # bash / Git Bash
```
```powershell
$env:PYTHONIOENCODING='utf-8'; uv run python ...    # PowerShell（开发机默认）
```

⚠️ PowerShell **不支持** `VAR=value cmd` 这种前置写法，会报
`无法将"PYTHONIOENCODING=utf-8"项识别为 cmdlet`。

交互式脚本 [scripts/try_questionnaire.py](scripts/try_questionnaire.py) 自己处理编码
（切控制台代码页 + reconfigure），**不需要**设这个变量。

---

# 第三部分 · 已完工的论证（勿回退）

> 这些都已定案并附实测依据。**除非有新证据，不要重新讨论。**

### 第 1 周 · 数据层 ✅

| 动作 | 状态 |
|---|---|
| 1. 拉 dump、摸清字段 | ✅ |
| 2. 筛候选集 | ✅ **11,453 部**，口径见 [src/candidates.py](src/candidates.py) |
| 3. 建表 + 灌数据 | ✅ `anime_profile` **11,453 行** + `alias` **38,378 行** |
| 4a. staff / studios（**改为走 dump**） | ✅ 10,576 / 10,688 部，见下 |
| 4b. AniList id / 英文名 / popularity | ✅ 6,445 部（56.3%） |
| 5. Tag 清洗 | ✅ **308 个题材 tag**，`data/interim/tag_vocab.json`（第二轮清洗后，见下）|

分词词典指纹 `6a1cbbe1bc4f446d`。⚠️ **API 启动时已强制比对**（`server/main.py`
的 `BUILD_FINGERPRINT`），不符直接拒绝服务 —— 不再是「第 3 周记得做」的待办。

### 第 2 周 · P0 推荐

| 动作 | 状态 |
|---|---|
| tag 向量矩阵 + mean-centered 打分 | ✅ [src/recommend.py](src/recommend.py) |
| 问卷选题 + 三种作答 | ✅ [src/questionnaire.py](src/questionnaire.py) |
| 续作折叠 | ✅ [src/series.py](src/series.py) + [scripts/build_series_map.py](scripts/build_series_map.py) |
| 交互式自测 | ✅ [scripts/try_questionnaire.py](scripts/try_questionnaire.py) |
| FastAPI 接口 | ✅ [server/main.py](server/main.py) + [server/schemas.py](server/schemas.py) |
| 打分迁 pgvector（放弃 Render） | ✅ [src/recommend_sql.py](src/recommend_sql.py) + [tests/test_parity.py](tests/test_parity.py) |
| 前端 v0 | ✅ [web/](web/)：问卷 + 推荐，评分存 localStorage |

📌 **前端刻意冻结在 v0（2026-08-13 定，2026-08-14 重申）** —— Kevin：
**先把逻辑全部做完，最后一次性写前端。**
没有路由、没有状态库、没有详情页，`/api/search` 与 `/api/anime/{id}` 已能用但界面没接。
**不要顺手美化它，也不要顺手接新接口。** 第 3–5 周的产出（embedding、选题、评测）
都会改变前端要展示的东西，现在打磨的 UI 大概率要重做。届时一次性重写比分次调整省事。

⬜ **前端欠账清单**（后端已就绪、界面没接的，最后一次性做）：

| 后端能力 | 前端要做的 |
|---|---|
| `GET /api/search`（BM25 + trgm 兜底） | 搜索框 + 结果列表 |
| `GET /api/anime/{id}` | 详情页 |
| `GET /api/questionnaire?exclude=...` | **多次作答**：把 localStorage 里已评分的 id 拼进 `exclude` |
| `experience` 三档（new/mid/veteran） | 让用户选资历，现在恒为默认值 |
| `mode`（season/aired/upcoming/classic/all） | 推荐模式切换 |

⚠️ 多次作答那条的前端逻辑：**只传有评分的，不传「没看过」的** ——
`to_rating()` 对 skip 返回 None、不产生记录，所以 skip 过的作品下轮会再出现，
这是有意的（用户当时没看过，过一阵可能就看了）。前端不需要为此多存一个集合。

矩阵 `(11453, 308)`。⚠️ **线上打分已不走这个矩阵** —— 余弦由 pgvector 在库里算
（见下「部署改道」）。内存矩阵保留给第 5 周评测的批量打分，两条路径由
[tests/test_parity.py](tests/test_parity.py) 锁死等价。全库暴力余弦实测 **≈ 0 ms**，
无论哪条路径都印证了第 4 节「不建 HNSW」的判断。

**已定的几件事：**

- **打分接口无状态**（第 2 节铁律）：`score(catalog, ratings, ...)`，评分随请求传入。
  本地自测用 JSON 文件、游客用 localStorage、注册用户用 `user_rating` 表，推荐链路一行不改。
  ⚠️ 所以**现在不建 user 表** —— 认证在第 6 周，现在建也没有东西会往里写。
- **权重 `log(1+count) × idf`，行 L2 归一化**。实测 binary 模式结果明显更差
  （日常档案的结果中位热度从 done=9,292 掉到 293），`binary` 保留给第 5 周做 ablation。
- **μ 向先验收缩**：`μ = (Σc·r + k·prior)/(Σc + k)`，`prior=7.07`（全站逐票均分）、`k=2`。
  ⚠️ 不收缩的话**只评一部或所有评分相同时偏好向量整个归零**，问卷答完第一题拿到空结果。
- **三种作答带不同置信度**（见 `questionnaire.to_rating()`）。
  ⚠️ 不区分的话「想尝试(8)」的权重会**高于**「看过并打 7.5」，显然反了。
  ⚠️ 「想尝试」的作品**要留在推荐结果里** —— 那正是用户要的；只有「看过」和「不感兴趣」才剔除。
- **排序 = 两段式召回 + blend**：先按匹配度取 `top_k × 10` 候选，再按
  `α·匹配 + (1-α)·贝叶斯加权评分` 重排（两者先在池内 min-max 归一化，量纲差太远）。
  α 默认 0.5：实测匹配度只掉 1.4%、结果均分涨 0.56。
  ⚠️ `rank_by="match"` 必须保留 —— 第 5 周评测要跑纯 tag 模型做对照。

⚠️ **零向量作品必须排除在候选之外。** 它们与任何偏好向量的余弦都是 0，
而偏好向量整体为负时（用户对问卷多数作品选「不感兴趣」），0 反而**高于**所有
负相关作品，Top5 会变成虫虫危机、隐形墨水这类没有 tag 的条目。

### ✅ 推荐结果加评分下限 `MIN_SCORE = 3.5`（2026-08-12）

**依据是评分信号的不对称性**：高分不保证好看（小众神作与过誉作品混在一起），
但低分几乎必然难看。所以低分可以当硬过滤，高分不能当硬排序。

⚠️ **和「热度权重必须等 baseline」不冲突，但要注意口径。** 那条讲的是把热度
**混进排序**会污染 NDCG 归因；这里是**候选池定义**，对四条 baseline 一视同仁。
`score(min_score=...)` 做成了参数，第 5 周**四条线必须传同一个值**（要么全默认、
要么全传 None），否则候选池不一致，NDCG 没有可比性。

⚠️ **判据是「有评分且低于 3.5」，未评分作品放行。** 写成 `>= 3.5` 会把
未评分作品一并排除，而 `mode="upcoming"` 推的正是还没播的新番。当前 dump 里
那 2 部碰巧有开播前评分（小圆剧场版 8.1、上低音号后篇 5.2），所以现在看不出问题，
但第 6 周季度同步接进真正的新公布作品后，upcoming 档会被**静默清空**。

⚠️ **必须用原始均分，不能用 `wr`。** wr 向 7.07 收缩，60 票打 3.0 的作品
wr 高达 6.39，按 wr 卡这条线等于什么都没过滤。

实测：库内低于 3.5 的共 **78 部（0.68%）**，最热门的是三体(1.70)、
兽娘动物园2(1.40)、约定的梦幻岛第二季(3.30)、国王游戏(2.70)，全是公认翻车作。
其中 26 部票数不足 100（最少 25 票），单看统计噪声偏大，
但误伤几部冷门作品的代价远小于推出一部烂片。

**为什么 tag 余弦特别容易踩这个坑**：烂续作的题材标签与前作几乎相同，
向量上和用户口味高度吻合 —— 烂的是执行不是题材。实测「只给约定的梦幻岛打 9 分」，
关掉下限时第二季以 **match=0.983 排在第 1 位**。

⚠️ **续作折叠挡不住这种情况，而且会加剧它。** 折叠逻辑是「换成系列里用户
**还没作答过**的最早一部」，而用户恰恰给第一季打了分 → 第一季作为「已看过」被剔除
→ 第二季反而成了该系列的入口。「给第一季打高分」正是最常见的情形。

实测各场景（默认配置）低分作品漏出数：

| rank_by | 开下限 | 关下限 |
|---|---|---|
| `blend`（用户实际看到的） | 0 | **0** —— 质量项已把低分压下去 |
| `match`（第 5 周 baseline 用） | 0 | **2**，含第 1 位 |

即 blend 模式下目前是空操作，但 blend 是 α=0.5 的**软加权**不是保证；
`match` 模式下它是唯一防线。

### Tag 词表第二轮清洗（2026-08-11，418 → 308）

第 13 节动作 5 预告的「正路」已经走通：**不再手工枚举人名公司名，改用 dump 的
person 数据自动检测**。方法分两层，缺一不可：

1. **共现检测**：打了 tag T 的作品，是否高度共享同一个 person？
   （`subject-persons` ∪ `person-characters`，后者必需 —— 声优走「作品→角色→声优」
   两跳，只查前者会把中井和哉、坂本真綾 全部漏掉）
2. **名字相似**：共享者的名字是否就是 T 的书写变体？

⚠️ **只做第 1 层会误杀真题材词。** 高产的窄领域创作者会制造假阳性：
`催眠` `肉感丰满` `辣妹` `足交` 全部指向同一个里番监督，`傲娇` 指向钉宫理惠，
`排球` 指向排球少年的原作者。这些都是真题材，必须保留。第 2 层用来区分。

⚠️ **第 2 层挡不住繁简变体**：`冨樫義博` vs `富坚义博` 字符重合率不到 20%。
这批（约 10 个）靠人工核对补上。彻底自动化要引 opencc，为这个规模不值得。

剔除的 110 个里：人名 ~55、IP ~30、公司 6、繁体形态词 4、观看状态/怀旧标记 9、地区 8。

### ✅ 问卷选题与推荐结果都要折叠续作（已实现，遗留一个缺口）

第 9 节已经警告过「问卷抽到系列续作等于白问一题」，但现有对策（限制在 `meta_tags ∩ {TV, WEB}`）**挡不住 TV 续作**。
实测问卷候选池 6,496 部（TV+WEB 且非 nsfw）里，**1,966 部（30.3%）是直接续作**，且越热门越严重 ——
按热度排前列的是 CLANNAD AFTER STORY、轻音少女第二季、进击的巨人第三季 Part.2、辉夜二三期。
而第 9 节的选代表逻辑是「热度权重要够大」，恰好会优先选中这批。

**判据（已实测定稿）：`subject-relations` 里有 `relation_type=2`（前传）指向的作品，且该前传播出不晚于本作 → 排除。**

⚠️ **不能简单按「有更早的关联作」排除** —— 那会误杀平行/外传作品。
`Fate/Zero` 与 `Fate/stay night` 是平行关系、`超电磁炮` 与 `魔法禁书目录` 是主线/番外，
这些都可独立观看，**应当保留**；只有「天之杯 第二章」「进击的巨人 第三季」这类必须看前作的才排除。
按 `rt=2` 判定实测正确：Fate/Zero、超电磁炮、轻音少女、进击的巨人、命运石之门第一部全部保留，各自续作全部排除。

⚠️ 「播出不晚于本作」这条补丁不能省：`rt=2` 是**故事顺序**不是播出顺序，
少了它会误杀 94 部先播后出前传的独立作品 —— Fate/stay night(2006)、游戏人生(2014)、咒术回战(2020)、狂赌之渊。

排除后可用 **4,530 部（69.7%）**，聚类选题绰绰有余。

**`relation_type` 码表（dump 无码表，2026-08-11 全量实测反推）**

只统计候选集内部（两端都是库内动画）的 14,980 条关系。**互为反向的码成对出现**，
这是确认方向性的关键证据：

| 码 | 含义 | 反向 | 条数 | 样本 |
|---|---|---|---|---|
| `2` / `3` | 前传 / 续集 | **互为反向，3,301 对** | 3313 / 3326 | 鲁鲁修R2 --2--> 鲁鲁修 |
| `6` / `12` | 番外篇 / 主线故事 | 互为反向，1,070 对 | 1075 / 1613 | 电影摇曳露营 --12--> TV版 |
| `4` / `5` | 总集篇 / 被总集 | 互为反向，393 对 | 393 / 395 | 高达 --4--> 高达剧场版 |
| `10` | 不同演绎版本 | 自反，978 对 | 1001 | 鲁鲁修R2 ↔ 剧场版三部曲 |
| `8` / `9` | 相同 / 不同世界观 | 各自自反 | 986 / 647 | 初代高达 ↔ V高达 / SEED |
| `7` | 角色出演 | 自反 | 940 | 人形电脑天使心 ↔ 翼·年代记 |
| `11` / `14` | 衍生 / 联动 | `11`→`12` | 529 / 76 | 高达 --11--> 高达桑 |
| `99` | 其他 | 自反 | 662 | 星际牛仔 ↔ Extra Session |
| `1` | 改编（跨类型） | 自反 | 24 | 主要指向书籍/游戏，库内动画间罕见 |
| `3001-3099` | 音乐关联 | — | — | OST、角色歌，指向 `type=3` |

同一份数据还能用于**推荐结果多样性**（别给看过第一季的人推第三季）。
✅ 2026-08-11 已实现：`questionnaire.select_items(fold_sequels=True)` 与
`recommend.score(fold_series=True)`，映射由 `scripts/build_series_map.py` 产出
（3,143 条续作 → 1,439 个系列根）。实测「厨力全开」档案的 6 条推荐从
覆盖 3 个系列变成 6 个，且推的是第一季而非第六季。

⬜ **遗留缺口：`rt=12`（主线故事）关系没有折叠。**
实测「电影 摇曳露营△」与 TV 版之间是 `rt=12` 而不是 `rt=2`，于是两者会同时
出现在推荐列表里。但**不能简单地把 rt=12 也纳入折叠** ——
`超电磁炮 --12--> 魔法禁书目录` 也是这个关系，而超电磁炮是可独立观看的外传、
热度还更高，折进禁书目录是错的。两者的区别在于标题是否同源，
需要额外信号（标题前缀相似度？）才能区分，留到第 6 周做多样性时再解。

### ✅ P0 的 tag 向量用 `meta_tags` 兜底（已实现，但效果有限 —— 142 部仍是零向量）

**166 部（1.4%）清洗后 `tags` 为空**（第一轮词表下是 139 部，二轮剔掉 110 个词后增加），
其中包括化物语（done=37,573）。
原因是 dump 的 `tags` 是**截断的 Top-11**，越有名的作品前 11 位越被制作组/监督/CV 占满：

```
化物语的 11 个 tag：新房昭之 西尾维新 化物语 SHAFT 2009年7月 荡漾 TV 战场原大人 花泽香菜 神谷浩史 2009
→ 题材词一个没有，清洗后 = 0 个
```

⚠️ **兜底效果远不如预期（2026-08-11 实测修正）。** 早先记的「这 139 部 100% 都有
`meta_tags`」字面没错，但**有 meta_tags ≠ 有题材类 meta_tags**。二轮清洗后
tags 为空的 166 部里，**只有 24 部（14%）**能靠 meta_tags 拿到非零向量，
其余 142 部的 meta_tags 全是形态+地区（`['TV','中国']`、`['剧场版','欧美','美国']`），
过 classify() 后被分流干净，仍是零向量。

没救回的以**欧美动画和国产老动画为主**：花木兰、狮子王、蓝精灵、虫虫危机、
大头儿子和小头爸爸、海尔兄弟 —— 官方题材标签对非日本作品明显更稀疏。
这 142 部在 P0 里**永远无法被 tag 余弦召回**，只能等第 3 周的 embedding
（简介文本人人都有）。

另外全库清洗后平均只有 **3.8 个**非零维 / 308 维（稀疏度 1.24%），
tag 向量比预想稀疏得多。

### 与本文档原计划的两处偏离（已论证，勿回退）

**① 动作 4 拆成 4a/4b，staff/studio 不走 AniList。**
实测 bangumi-data 能给出 AniList id 的只有 56.4%，且缺口不随机：国产 7.1%、欧美 0.1%、R18 3.0%、OVA 25%。
而 dump 自带的 `subject-persons` + `person` 覆盖 92.3%（公司）/ 93.3%（staff），国产 83.6%、欧美 58.2%。
「喜欢上海美术电影制片厂」是真实口味维度，只走 AniList 会把它整个抹掉。
AniList 保留它真正独有的：`idMal`（Phase 2 锚点）、英文名、全球热度。

**② `search_tsv` 不含 `summary`（2026-08-11）。**
它一项占全库 33%：中文分词后 2.7 MB 原文膨胀成 15 MB tsvector + 17 MB GIN 索引。
剧情关键词检索改由第 4 周 HyDE + 向量混合检索负责。`summary` 列本身保留（第 3 周 embedding 输入）。
同期还删了 `idx_alias_trgm`（9.9 MB，3.8 万行顺序扫描足够快，一条 SQL 可重建）。
**库因此从 99 MB 降到 43 MB，没有删任何一部作品** —— 删光里番只省 4 MB，删 `done<200` 再省 8.8 MB，代价却是 30% 语料和第 5 周的评测信号。

---

# 第四部分 · 待办（按优先级）

### ⬜ `staff_vec` 缺声优 —— P1 特征层的已知缺口（2026-08-14 记）

**Kevin 认为最要紧的因素是：声优 · 导演 · 音乐 · 制作公司。**
其中三项已经有了，缺的只有声优：

| 因素 | 现状 | 维数 |
|---|---|---|
| 导演 | ✅ | 370 |
| 音乐 | ✅ | 284 |
| 制作公司 | ✅ | 284 |
| **声优** | ❌ **完全缺失** | — |
| 脚本 / 人物设定 / 原作 | ✅ 有，但 Kevin 认为不要紧 | 423 / 348 / 224 |

⚠️ **声优不在 `subject-persons` 里，所以 `backfill_staff.py` 再怎么改都拿不到。**
dump 里声优走的是**两跳**：`subject-characters`（作品→角色）+
`person-characters`（角色→声优）。第 13 节 tag 二轮清洗时用过这条路径 ——
当时正是因为只查 `subject-persons`，把中井和哉、坂本真綾 全漏了。

⇒ 要补声优得：`subject-characters ⋈ person-characters` 两跳聚合 →
按角色重要度（主角/配角）截断 → 并进 `staff` 列的 `role='声优'` →
重跑 `build_staff_vectors.py`（词表和 `sparsevec(N)` 的维度都会变，
**sql/006 的列宽和 staffvec.DIM 必须同步改**）。

📌 **为什么这条值得记**：第 5 周评测若发现 staff 那一路贡献不明显，
**这是第一个该查的原因** —— 而不是急着调 γ 权重。
「喜欢花泽香菜配的角色」是真实且常见的口味维度，现在整个抹掉了。

📌 **美术（美術監督）已明确不做**（2026-08-14 Kevin 定）。
（实测 dump 里还有十几个未映射的岗位码，美术监督大概率在其中，
所以这是「不做」而不是「做不了」—— 将来改主意的话是映射问题不是抓取问题。）

### ⬜ 推荐结果要加热度权重 —— 但**必须等 baseline 跑完再加**（第 5 周后）

已确认需要：纯 tag 余弦没有热度先验，冷门作品只要那 3–4 维恰好对上就能拿高分。
实测「硬核科幻」档案的 Top5 里出现 `宇宙战舰大和号 新的旅程`(done=105)、
`永远的大和号`(done=92) 这类几乎无人看过的作品。

⚠️ **但不能现在加。** 第 10 节把「热度」列为四条 baseline 之一，
现在混进去，第 5 周就分不清 NDCG 的提升来自 tag 模型还是来自热度先验 ——
那恰恰是评测要回答的问题。跑完 baseline 拿到对照数据再决定权重形式。

（2026-08-11 决定：Kevin 认为热度权重有必要，但同意排在 baseline 之后。）

### ⬜ `轻小说改` 与 `小说改` 高度共现，等于同一信号算两遍（第 3 周 P1 时看）

推荐结果里这两个 tag 几乎每条都同时出现。第 13 节明确写了两者**不合并**
（一般小说 vs 轻小说，受众不同），这条不变；但**共现**没处理 ——
一部轻改作品在向量里会同时点亮两维，相当于把「轻改」这个信号加权了两次，
放大了轻改类作品之间的相似度。

同类嫌疑还有 `异世界`+`穿越`、`后宫`+`校园`。第 3 周做 P1 时可以顺手统计
全库 tag 共现矩阵，看哪些对的 PMI 高到该做降权。**现在不动** ——
调权重会污染第 5 周 baseline 的口径。

### ⬜ 「未开播」档的档期数据太稀疏（第 6 周季度同步时解）

`--mode upcoming` 实测只召回 2 部。dump 是静态快照，对未来季度天然滞后，
而「这季看什么」最需要的恰恰是还没播的。要靠第 6 节的季度增量同步
（走 Bangumi API 而非 dump）补齐。

### ⬜ `anime_profile.vec` 建议用 `halfvec(1024)` 而非 `vector(1024)`（第 3 周）

省 23 MB（47 → 23.5 MB），fp16 对余弦相似度的影响可忽略，且第 4 节给 `plot_chunk` 本来就选的 halfvec。

### ⬜ `idx_profile_extids` 目前无人使用

GIN on `external_ids`，608 kB，`idx_scan = 0`。为 Phase 2 的 `external_ids @> '{"mal":123}'` 准备。存储吃紧时可先 DROP。
---

# 第五部分 · 设计文档（第 1–15 节）

> ⚠️ **这是最初的设计稿，不是当前状态。** 保留是因为其中的**论证过程**仍然有用，

> 但部分**结论**已被一~三部分的实测推翻，已知处都加了标注。

> ⚠️ **编号是稳定锚点，全文几十处交叉引用依赖它，不要重新编号。**

## 1. 项目定位

**要解决的问题：** 每季新番 50–80 部，用户无法判断哪些符合自己口味。

**做法：** 用户对看过的动画评分 → 系统学习偏好 → 预测当季新番匹配度。评分通过偏好引导问卷收集。

**两种使用方式：** 游客直接用（评分只存浏览器，关掉即弃），或注册账号（评分入库，跨季度累积，越用越准）。**注册不是使用门槛**——游客能用全部功能。

**性质：** 简历/作品集项目，有后续实际部署的可能。**离线评测的严谨性是核心卖点**，优先级高于功能数量。

---

## 2. 技术栈（已定，勿改）

> 🚫 **本节的「后端 = Render」已作废（2026-08-12）。** 现行部署是 Vercel serverless，
> 见 **第二部分 B**。下面那段 Render 选型分析（Hobby 工作区 vs Starter 实例、
> 为什么否决 AWS/Supabase/Clerk）**保留是因为论证过程仍然有效** ——
> 「免费实例休眠 50 秒会赶走招聘方」这个理由本身没变，
> 只是解法从「买一个不死的进程」换成了「让进程不再需要活着」。
>
> ⚠️ 别照着这张表去买 Render。**「双轨会话」小节仍然有效**，未被推翻。

| 层 | 选型 | 备注 |
|---|---|---|
| 前端 | React + TypeScript + Vite + Tailwind | 部署 Vercel |
| 后端 | FastAPI (Python) | ~~Render Hobby 工作区($0) + Starter 实例($7/月)~~ → **Vercel serverless** |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1 | 免费层，0.5 GB/project，区域 us-east-2 |
| 缓存 | Upstash Redis | 缓存 HyDE 改写结果 |
| CI/CD | GitHub Actions | 季度数据同步 |
| 对象存储 | Cloudflare R2 或 Vercel 静态资源 | 存 chunk 正文 |
| 认证 | 邮箱+密码自建，argon2 + JWT | 存自己的 Neon，不引入第三方 |

**部署决策依据：**
- Render 免费实例 15 分钟休眠、冷启动约 50 秒 → 招聘方点开会直接关掉，必须付费实例
- ⚠️ **别被 Render 的定价页绕进去：工作区计划和实例类型是两笔账。** Hobby 工作区标价「$0/mo **plus compute costs**」，那个 plus 后面才是重点——Starter 实例 $7/月（512 MB / 0.5 CPU、不休眠）在 Hobby 工作区里就能选。下一档 Professional($19/user/月) 是**工作区**计划，卖的是团队协作功能，和「服务不休眠」无关，不需要
- 2026-08-01 起 legacy Hobby 计划已强制迁移到新计划，同时带宽从 100 GB 降到 **5 GB**（超出 $0.15/GB）。对本项目无影响——后端只吐 JSON，chunk 正文按设计走 R2/静态资源不经过 Render。**但这条是「正文别改成从 FastAPI 返回」的又一个理由**
- ⚠️ **Render 服务区域必须选 Ohio 或 Virginia**，与 Neon 的 `us-east-2` 对齐。真正影响性能的是 API↔DB 延迟（每个请求都要打几次），不是开发机到 DB 的延迟（只在灌库时有感）。跨区能轻易多出 50–100 ms/查询
- AWS 被否决：配置成本远超收益，除非投基础设施岗
- Supabase 被否决：免费层同样是 500 MB 数据库，且 7 天无活动**自动暂停且需手动恢复**，失败模式比 Neon 的 scale-to-zero 更糟；付费 $25/月 vs Neon 约 $2/月
- Clerk/Auth0 被否决：理由同 Supabase——为了省几十行代码引入新供应商和 MAU 上限，不划算
- OAuth 被否决：要为本地开发、Vercel preview、生产各配一套回调域名，调试成本高于收益；且动画用户不一定有 GitHub

### 双轨会话（已定）

| | 游客 | 注册用户 |
|---|---|---|
| 评分存哪 | 仅 localStorage，**服务端零写入** | `user_rating` 表 |
| 关掉浏览器 | 数据没了 | 保留 |
| 能用的功能 | 全部（推荐/检索/问答） | 全部 + 跨会话累积偏好 |

⚠️ **架构铁律：打分接口从第 2 周就设计成「评分向量随请求传入」的无状态形式。**
服务端只管「给一组评分 → 算推荐」，评分从 localStorage 来还是从 DB 来，是上面一层的事。
这样第 6 周加账号系统只是多一个数据来源，不用回头重写推荐链路，游客和注册用户也永远走同一条打分代码路径（少一半 bug，评测时也不会出现两套口径）。

**游客转正：** 注册时把 localStorage 里已有的评分批量迁移进账号，不让用户白答一遍问卷。

✅ **问卷多次作答已就绪（2026-08-14），第 6 周不用再动选题逻辑。**
`select_items(..., exclude=...)` 与 `GET /api/questionnaire?exclude=` 已实现，
调用方传入已评分的 id 即可跳过它们继续取题。⇒ 第 6 周要做的只有三件：

1. 登录后从 `user_rating` 取已评分 id，填进 `exclude`（游客侧是 localStorage）
2. 问卷答完把结果 upsert 进 `user_rating`，`source='questionnaire'`
   （该字段 §4 早已为此预留 —— 问卷引导打的分与主动搜索打的分置信度不同）
3. 转正时把 localStorage 的评分批量迁移

⚠️ **一个用户可以多次作答，所以 `user_rating` 会累积多轮 `source='questionnaire'`
的行。** 主键 `(user_id, subject_id)` 保证同一部番只留最新分，重复打分走 upsert
—— 这条设计正好接得住，不用改 schema。

---

## 3. 数据源

### 主数据源

**Bangumi Archive（首选）** — `github.com/bangumi/Archive`
- 官方定期导出全站 wiki 数据
- 从 `aux/latest.json` 获取最新 dump 地址（含 sha256，务必校验）
- ⚠️ 导出的是**原始 wiki 字符串**，不是解析好的 JSON。用官方 `wiki-parser-py`（PyPI 包名 **`bgm-tv-wiki`**）解析，不要自己写正则。作者声明无向后兼容承诺 → 必须锁版本
- 一次下载 >> 几千次限流 API 请求。API 只用于后续季度增量同步

#### 实测结论（2026-08-10，dump-2026-08-04）

压缩包 **410 MB**（不是早期估计的 200 MB），解压 1.8 GB，共 **673,996** 条 subject。

| 文件 | 解压大小 |
|---|---|
| subject.jsonlines | 946 MB |
| episode.jsonlines | 334 MB |
| character.jsonlines | 160 MB |
| subject-persons / person / subject-relations | 150 / 70 / 74 MB |
| subject-characters / person-characters / person-relations | 27 / 23 / 8 MB |

**`type` 映射（已实测确认）：** `1`=书籍(410,252) `2`=**动画**(30,610) `3`=音乐(99,216) `4`=游戏(107,896) `6`=三次元(26,022)。`type=5` 不存在。

**subject 顶层字段：** `id` `type` `name` `name_cn` `infobox`(原始 wiki 串) `platform` `nsfw` `tags`(**已结构化** `[{name,count}]`) `meta_tags` `score` `score_details`(1–10 分布直方图) `rank` `date` `favorite`(`wish/done/doing/on_hold/dropped`) `series`

**三个省事的发现：**
1. **评分和收藏数都在 dump 里** — `score` / `score_details` / `rank` / `favorite` 齐全，**不需要额外调 API**
2. **`tags` 已经是结构化数组**，不用从 infobox 抠
3. **`meta_tags` 是官方规范化标签，动画覆盖率 88.7%** — 形态(TV/WEB/剧场版/OVA/短片)、地区(日本/中国/欧美)、来源(漫画改/原创/小说改/游戏改)、题材(奇幻/战斗/恋爱)。噪声只存在于**用户 `tags`**，meta_tags 干净可直接用

**`date` 格式：** type=2 中 83.7% 是完整 `YYYY-MM-DD`，16.3% 为空，**没有任何残缺格式**（不存在 `2011-01` 这种）→ 解析不需要容错分支。

⚠️ **但 `date` 为空不等于该丢弃 —— 必须回退到 infobox。** 实测：满足其余全部候选条件、仅缺 `date` 的有 **213 部，其中 207 部（97%）的日期就在 infobox 里**（字段名为 `放送开始` / `上映年度` / `发行日期` 等）。而这批**几乎全是上海美术电影制片厂的国产经典**：

```
done= 9,935  大闹天宫      done= 7,299  葫芦兄弟      done= 4,769  黑猫警长
done= 3,108  九色鹿        done= 2,738  天书奇谭      done= 2,288  小蝌蚪找妈妈
```

直接按 `date` 为空丢弃，「经典回顾」模式（第 7 节）就会缺掉中国观众最核心的童年经典。回退逻辑在 [src/candidates.py](src/candidates.py) 的 `parse_year()`，用官方 parser 读 infobox，**不自己写 wiki 语法正则**。

**`platform` 映射（动画）：** `1`=TV `2`=OVA `3`=剧场版 `5`=WEB `0`=未设置。与 meta_tags 形态标签互相印证。

⚠️ **不要用 platform 兜底 meta_tags 缺失。** 已实测：2011+ 中无形态标签的 2,282 条里 done≥50 的只有 318 条，platform 也多为「未设置」，内容是同人动画(`幻想万华镜`系列)、MV、概念影像，**不是商业新番**。兜底只能捞回 12 条，不值得增加复杂度。

**AniList GraphQL** — 补充 staff/studio 结构化数据、英文标题、popularity
- 按分钟限流，11,453 部分页拉约 150 次请求，加 sleep

**ID 映射** — `bangumi-data` 开源映射表
- ⚠️ 不要用标题模糊匹配，同名作品和季度后缀会出错

### 补充语料

**萌娘百科（Moegirl）** — 仅作剧情问答语料，第 4 周之后才碰
- 系列条目 + **角色条目**（角色页内容常比系列页更丰富）
- 走 MediaWiki API，礼貌速率 1 req/s，10,000 篇约 3 小时
- 剧透标记：利用其现成的 `heimu` CSS class 离线打标

### 已否决

- **H萌**：法律风险，无 API

---

## 4. 数据库 Schema

五张表：三张数据表（第 1 周建）+ 两张用户表（第 6 周建）。

### 数据表

**`alias`** — 动画与角色实体
- 角色行通过 `parent_subject_id` 关联到作品
- ⚠️ 角色消歧必须**锚定在已确认的 `subject_id` 范围内**，否则跨作品同名角色会撞车

**`anime_profile`** — 每部动画一条
- 向量列：`vector(1024)`，float4
- **不建 HNSW 索引** — 1.1 万条暴力算余弦仅几毫秒，且是精确检索，比近似索引召回更准
- 新增列：`cluster_id`（聚类选题用）

**`plot_chunk`** — 章节级剧情切片
- 字段：`subject_id`, `character_id`, `section_title`, `episode_no`, `spoiler_level`, `source_type`, `source_work`, `content_ref`, 向量
- 向量列：`halfvec(512)` + HNSW 索引
- ⚠️ **正文不存 Neon**，只存 `content_ref` 指向 R2/静态 JSON

### 用户表（第 6 周建，schema 现在定好）

**`app_user`** — 账号
- `id`, `email`(unique), `password_hash`, `created_at`, `last_login_at`
- ⚠️ 表名**不能叫 `user`**，那是 Postgres 保留字，不加引号查询会报错
- 密码用 **argon2id**，不用 bcrypt/sha256。MVP 不做邮箱验证，但 `email` 加 unique 约束
- 不存任何 PII，不存昵称头像。账号唯一作用是绑定评分

**`user_rating`** — 个人评分
- `user_id`, `subject_id`, `score`(1–10), `source`('questionnaire' | 'manual'), `created_at`, `updated_at`
- 主键 `(user_id, subject_id)` — 同一部番只留最新分，重复打分走 upsert
- `source` 要留：问卷里被引导打的分和用户主动搜出来打的分，**置信度不一样**，后续做加权时会用到
- 「没看过」不写行，用缺失表示，不要用 `score = 0` 占位

### 为什么两种粒度、两种维度

一部动画一个 profile 向量用于**发现**；章节级 chunk 用于**剧情问答**。两者用途不同，维度也没理由统一——profile 只有 1.1 万条，1024 维才 47 MB，完全不构成压力；真正吃存储的是 10 万条 chunk。

---

## 5. 存储预算（硬约束）

Neon 免费层 **0.5 GB/project，超限直接挂起项目而非计费**。

| 项 | 大小 | 状态 |
|---|---|---|
| `anime_profile` 元数据 + `search_tsv` + 索引 | **26 MB** | ✅ 实测（vec 尚空） |
| `alias`（38,378 行 + 索引） | **10 MB** | ✅ 实测 |
| 系统目录等 | ~9 MB | ✅ 实测 |
| `anime_profile.vec` **halfvec(1024)** | +23.5 MB | ⬜ 第 3 周 |
| plot_chunk 向量 **halfvec(512)**，10 万条 | 102 MB | ⬜ 第 4 周 |
| plot_chunk HNSW 索引 | ~140 MB | ⬜ 第 4 周 |
| app_user + user_rating（1 千用户 × 200 条评分） | ~10 MB | ⬜ 第 6 周 |
| **合计** | **~334 MB / 500 MB（余量 33%）** | |

### ✅ 维度已定：profile 1024 / chunk 512（2026-08-14）

**两层维度不统一是刻意的**，理由是存储差距在两层上差两个数量级：

| | profile（11,453 条） | plot_chunk（10 万条） |
|---|---|---|
| 1024 | 23.5 MB | 205 MB + ~240 MB 索引 = **445 MB** ❌ |
| 768 | 17.6 MB | 154 MB + ~185 MB = 339 MB ❌ |
| 512 | 11.7 MB | 102 MB + ~140 MB = **242 MB** ✅ |

profile 上 1024→512 只省 11.7 MB（预算的 2.3%），不值得为此让掉 MRL 截断的
1–3% 质量；chunk 上 512 是唯一放得进预算的选择。

⚠️ **768 是最差的选择** —— profile 上为省 1.2% 预算白让 1% 质量，
chunk 上又省得不够。这是「折中」的典型失败形态：两头不讨好。

💡 **两层维度不同完全不成问题**：profile 向量和 chunk 向量**从不互相比较**，
各自只和查询向量比。一次 API 调用就能服务两个索引 —— 拿 1024 维查询向量，
搜 profile 用完整的，搜 chunk 时截断到 512 再归一化（MRL 截断合法，零额外成本）。

💡 **而且这个决定可逆**：缓存永远存 1024（见 A.9），要改维度或跑第 10 节那个
「512 vs 1024 ablation」，从缓存重放即可，不用重新请求 API。

⚠️ **原表的问题（2026-08-11 修正）：** `alias` 表整个漏记了（它 10 MB，索引就占 5 MB）；
「anime_profile 47 MB」实为向量部分的估算，而元数据+索引本身又是 26 MB，两者要相加不是二选一。
真实已用 **44 MB**，与原表「元数据+BM25 索引 20 MB」的估计差一倍多。

⚠️ **MVCC 膨胀会虚增占用。** 每轮批量 UPDATE 都会留下旧行版本：三轮回填后库从 43 MB 虚涨到 99 MB，
`VACUUM FULL` 后回到 44 MB。**批量回填之后记得跑一次**，否则会误判预算已经吃紧
（普通 `VACUUM` 只把空间标记为可复用，不归还 OS）。

用户数据对存储预算**不构成威胁**：一行评分几十字节，就算 1 万用户各打 200 分也才 100 MB 量级，而作品集项目不会有 1 万用户。真正吃存储的仍然是 chunk 向量和它的 HNSW 索引。

### ⚠️ Neon 的存储计量 ≠ `pg_database_size()`（2026-08-14 实测）

```
pg_database_size()  86 MB      ← 我们一直在看的数
Neon 控制台         110 MB     ← 真正计入配额的数
```

**Neon 计的是「当前数据 + 保留的历史」**，因为它的架构是写时复制的页存储 +
PITR 历史窗口。⚠️ **所以 500 MB 的预算要按控制台的数算，不是按 SQL 查出来的数。**

那 24 MB 的差主要是**当天的写入churn**：实测 WAL 累计写入 **1,150 MB**，
来自灌 `vec`(10,864 行) + `staff_vec`(10,269) + `mmr_rank`/`cluster_id`(4,439×2)
和**两次 VACUUM FULL**。历史窗口过期后会缩回去，不是永久的 28% 附加税。

#### ⚠️ VACUUM FULL 在 Neon 上比在普通 Postgres 上贵得多

它**重写整张表的每一个页面**，产生等于表大小的 WAL —— 而在 Neon 上
WAL 会进入存储计量和历史保留。今天两次 VACUUM FULL 约贡献 150 MB WAL。

⇒ **别把它当成免费的清理动作**。第 4 周灌 10 万 chunk 时尤其注意：
本节前面已经写了「分批灌 + 每批后 VACUUM」，那条现在有了第二个理由 ——
不只是怕全表重写顶穿上限，也是因为**每次全表 VACUUM FULL 都在烧存储配额**。
普通 `VACUUM`（不带 FULL）不重写页面，代价小得多，优先用它。

### ⚠️ Neon 还有一条独立配额：网络传输 5 GB/月（2026-08-14 踩到）

存储不是唯一的硬约束。**一天之内跑掉 2.68 GB**，原因是 `build_catalog()`：

| | 单次传输 | 耗时 |
|---|---|---|
| 文本格式（默认） | **~150 MB** | 7.8 s |
| binary 格式 | ~36 MB | 1.9 s |
| **本地缓存命中** | **0** | 0.65 s |

⚠️ **`vec` 列文本格式 132 MB / 二进制 21 MB —— 膨胀 6 倍。**
P1 之前 `build_catalog()` 只拉 `tag_vec`（文本 7 MB），便宜 20 倍；
是 P1 把 1024 维的 `vec` 加进这个查询才让它变贵，而格式仍是默认的文本。

⇒ 两处修复（[src/recommend.py](src/recommend.py)）：
1. `conn.cursor(binary=True)` —— 4 倍
2. **本地 npz 缓存** `data/interim/catalog_cache/`，键含 `build_meta` 的三个
   指纹 + 各列非空行数，任一向量列重灌过键就变 —— 命中时网络开销为 **0**

⚠️ **缓存不是优化而是配额保护。** 开发时每轮 pytest 触发一次、
第 5 周 leave-one-out 要反复构建 —— 没有它，评测跑几轮就会撞穿月度配额。

💡 **线上不受影响**：`/recommend` 单次响应 4.6 KB，只取被评作品的向量
（几十行）+ 无向量的召回池（200 行）。这条配额的压力**全部来自离线路径**。

### ⚠️ 真正会失控的变量是 chunk 条数，不是维度

维度已定死，剩下唯一能把预算撑爆的是**切分粒度** —— 「10 万 chunk」是拍的，
把 chunk 从 400 字改成 200 字，条数直接翻倍。

按每条 chunk 约 **2,400 字节**（512 维 halfvec 1,024 B + HNSW 索引摊销）算：

```
可用于 chunk 的空间 ≈ 450 − 110(第3周末，**Neon 口径**) − 10(用户表) = 330 MB
                    ÷ 2,400 字节/条
                    ≈ 15 万条
```

📌 **天花板约 13.7 万 chunk，计划 9 万，余量 52%。**
（原写 15 万，是按 `pg_database_size()` 算的；改用 Neon 口径后收窄。）
这个数比「余量 33%」更有用 —— 它直接是第 4 周调切分策略时的红线。

（450 而非 500 是留给 VACUUM/WAL 的安全垫，见下。原文此处写「20 万以上」是早期
粗估，未按 halfvec(512) + 索引摊销重算，以本节的 15 万为准。）

### 触线时的三级阀门（按代价从低到高）

```
1. 收紧 chunk 切分粒度   ← 零成本
2. 砍批次 3（角色页 ~6 万条）← 省 145 MB，且第 11 节本来就标记为「可砍」
3. 拆库                  ← 最后手段
```

⚠️ **第 2 级比第 3 级便宜得多**，而且它本来就是排期上的可选项
（第 11 节：「时间不够就不做，不影响主线和评测」）。**先用它，别急着拆库。**

**逃生方案（第 3 级，暂不启用）：** Neon 免费层给的是每 project 独立 0.5 GB，可拆成：
- Project A：`alias` + `anime_profile`（发现层）
- Project B：`plot_chunk`（语料层）

两边**本来就不需要 JOIN** —— 剧情问答流程是「先确认作品拿到 `subject_id` →
在该作品内检索 chunk」，跨库只传一个整数。

⚠️ **但代价对本项目特别重**：流程 C 一个请求里要用到两个库，
即**一次请求唤醒两个 compute**。而「低流量项目大部分请求都是冷启动，不是偶尔」
这条自测结论在这里正好反着咬 —— 冷启动翻倍不是偶发成本而是常态成本。
⚠️ 另外 Neon 免费层除存储外还有 compute 用量限制，两个 project 是两份 compute。
⬜ 当前条款未核实，真要拆之前必须先查。

⚠️ 若真拆库，对外表述是「按访问模式拆分发现层与语料层」，不是「绕开免费层限制」。

### ⚠️ 灌 chunk 要分批灌 + 每批后 VACUUM，不要攒到最后

`VACUUM FULL` 是**重写整张表**，过程中短暂占用约两倍空间。
第 3 周库才 81 MB 无所谓，但第 4 周到 324 MB 时，对 `plot_chunk` 做一次
全表 `VACUUM FULL` 可能直接顶穿上限 —— **而 Neon 超限是挂起项目不是计费。**

---

## 6. Embedding 方案

> ⚠️ **本节的「两条通路 + 一致性验证」已被 A.7 取代（2026-08-14）。**
> 现行方案是**全程走 API + 本地缓存层**，理由：线上只能是 API（Vercel 跑不了模型，
> 见 A.6），所以两条路径不一致时唯一可行的收敛方向就是把建库也搬到 API 上。
>
> ⚠️ **本节末尾「低于 0.999 说明有归一化差异」那句归因是错的。**
> 余弦对缩放不变，纯归一化差异会让测试**恰好等于 1.0**。真正的漂移源是
> 池化方式 / instruct 前缀 / 模型版本被替换。详见 A.7。
>
> 模型选型（Qwen3 而非 bge-m3）与 BM25 预分词那两段**仍然有效**。

**模型：Qwen3-Embedding-0.6B**

选它而非 bge-m3 的原因：bge-m3 固定 1024 维、无 Matryoshka 训练，截断降维会掉点；Qwen3 原生支持 [64,128,256,512,768,1024] 自定义维度。损失的 sparse 检索能力由 Postgres 侧的 BM25 补上——但**实现方式和原计划不同，见下**。

### ⚠️ 中文 BM25：必须在 Python 侧预分词（2026-08-10 实测修正）

原计划「BM25 走 Postgres tsvector」**直接用是不成立的**。Neon 上实测：

```
to_tsvector('simple',  '轻小说改编的奇幻冒险动画') → '轻小说改编的奇幻冒险动画':1   ❌ 整句一个 token
to_tsvector('english', 同上)                      → 同样一个 token                ❌
```

Postgres 内置分词器全部按空格/标点切词，**没有任何一个能切中文**（可用配置只有 arabic…yiddish 那 30 个语言，无中文）。而 Neon 作为托管服务**装不了 `zhparser` / `pgroonga`** —— `pg_available_extensions` 里只有 `vector` 和 `pg_trgm`。

**解法：入库前用 jieba 切好词、空格连接，再 `to_tsvector('simple', ...)`**
```
'轻小说 改编 的 奇幻 冒险 动画' → '冒险':5 '动画':6 '奇幻':4 '改编':2 '的':3 '轻小说':1   ✅
```

⚠️ **纪律：建库与查询必须用同一个分词器 + 同一套自定义词典。** 版本或词典漂移会导致查询词切出来和库里对不上，召回直接崩。性质等同于第 6 节的 embedding 一致性陷阱，**同样要在建库前验证**。

💡 **jieba 自定义词典直接复用清洗后的 308 个 tag 词表。**「轻小说」「异世界」「泡面番」「萝卜」这类动画圈专有名词通用词典会切错，而它们全在 tag 词表里，零额外成本。

**`pg_trgm` 是另一件事，别混用：** 中文相似度实测 0.438（字符级 trigram），可用于 `alias` 表的**别名模糊匹配兜底**，但做不了 BM25 的相关性排序。

**两条通路：**
- **离线建库**：本地/Colab 批量跑（0.6B，CPU 也能跑），不占任何 API 配额，不怕限流
- **线上 query**：硅基流动 API（bge/Qwen 系 embedding 模型免费）

⚠️ **建库前必须验证一致性**：拿 20 条文本本地和 API 各跑一遍，对余弦相似度。低于 0.999 说明有归一化差异，必须统一走一边。

**成本参考：** ⚠️ 本段的估算已被 A.7 的实测取代 —— 实际走硅基流动
`qwen3-embedding-0.6b` 是 **¥0.07/百万 token**，全量（含 profile + 10 万 chunk）
约 **¥1.92 / 27 分钟**，不是这里写的 20 元。**结论不变且更强：成本不是瓶颈，存储才是。**

---

## 7. 三个功能

1. **推荐系统**（主线）
2. **自然语言检索** — HyDE query 改写 + BM25/向量混合检索
3. **剧情问答** — 基于萌娘百科语料，带剧透控制

### 推荐模式：新番 / 经典回顾（2026-08-10 新增）

用户可选推荐的时间范围：

| 模式 | 范围 | 场景 |
|---|---|---|
| **当季混合** `season` | 前一季起点−7天 ~ 后一季终点 | 原始定位：50–80 部里挑哪些合口味 |
| **已开播** `aired` | 同上 ∩ `air_date ≤ 今天` | 现在就能看的 |
| **未开播** `upcoming` | 同上 ∩ `air_date > 今天` | 提前规划这季追什么 |
| **经典回顾** `classic` | 2011 年前 | 年轻用户没看过《狼与香辛料》，但想补经典 |
| 任意年份区间 | `year_min` / `year_max` | — |
| 不限 `all` | 全部 | — |

✅ 2026-08-11 已实现于 `recommend.score(mode=..., year_min=..., year_max=...)`。

⚠️ **「当季」只能按日期窗口定义，不能把每部番归类到某个季度。**
实测：TV+WEB 作品 **75.4%** 集中在 1/4/7/10 月开播（4月21.8% 10月20.1% 7月16.9% 1月16.6%），
季度结构是真实的；但季度**前一个月**（3/6/9/12）下旬开播的占 36–53%，
而季度月本身只有 6.5–9.7%。看上去该把这批归到下一季，实则不然 ——
12 月下旬那批**大多是年末特番**（猫物语（黑）、卫宫家今天的饭、齐木楠雄完结篇、
FGO First Order），不是 1 月番；3 月下旬则是混合的，
`我的英雄学院 第二季`(2017-03-25) 确实是提前开播的 4 月番。**逐部归类做不可靠。**

改用日期窗口后，「已开播/未开播」直接比 `air_date` 和今天，精确无歧义；
只有「混合」档需要定起点，往前宽限 **7 天** 即可接住抢跑的季番。
⚠️ 这 7 天取自行业惯例而非数据拐点 —— 实测「提前 N 天开播」是平坦长尾，
唯一的尖峰在「提前 1 天」，恰恰是那批年末/季末特番，放宽到它们没有意义。

**⚠️ 前置依赖：候选集必须取消 2011 年下限。** 狼与香辛料(2008)、灼眼的夏娜(2005) 都在原口径之外，不扩就是空功能。见第 13 节动作 2。

#### 这个功能不只是个筛选器 —— 两点非显而易见的价值

**① 它把项目变成两个不同的推荐问题，而不是一个问题加个 WHERE**

- **新番推荐 = item cold-start**。新作品没有任何交互数据，共现矩阵里是空行，**只能靠内容特征**（tag 向量 / embedding）
- **经典回顾 = 数据充分**。有完整的评分和收藏历史，协同过滤、共现簇全都能用

同一套偏好向量，两条不同的召回路径。**能把这个区分讲清楚，比多做三个功能更能体现对推荐系统的理解。**

**② 它让第 10 节的离线评测变得更严谨，而不是更麻烦**

两种模式对应两种评测协议：

- **经典回顾模式** → 标准 leave-one-out（第 10 节现有方案）
- **新番模式** → 必须用 **temporal split**：取时间点 T，只用 T 之前的交互训练，预测 T 之后新番的接受度

后者比随机 LOO 更贴近真实场景，也更难作弊——随机 LOO 会泄漏"未来信息"（用同一部番后期的评分预测早期），temporal split 不会。**「我们对两种场景用了不同的评测协议，因为随机划分会泄漏未来信息」这句话，比多报一个 NDCG 数字更有说服力。**

#### 设计约束

⚠️ **模式选择只影响推荐结果的过滤，不影响偏好学习。**

一个容易搞反的点：选「经典回顾」的用户，恰恰是**没看过老番**的人（所以才想补）。他的偏好只能从看过的作品（多半是新番）推断。所以：

- 偏好向量的计算**与模式无关** —— 和第 2 节的「架构铁律」一致，服务端只管「给一组评分 → 算偏好」
- 模式只作用在**候选池过滤**这一步
- 问卷选题**不按模式变化**，仍从高热度作品里选，混合年代反而覆盖度更好

⚠️ **经典回顾模式要防「只推神作」。** 老番经过时间筛选，留下的普遍高分，纯按预测分排序会退化成一张 IMDb Top 250。需要引入多样性约束或按用户偏好向量的距离而非绝对分排序 —— 第 5–6 周实现时注意。

**排期：** Phase 1.5。数据层（取消年份下限）第 1 周顺手做掉，UI 和召回逻辑在第 6 周或六周后。

**剧透 UX（已定）：** 检索前门控 — 先做非剧透检索 → 用户显式确认 → 再检索剧透内容并生成。**不是**生成后用 LLM 检测。理由：萌娘百科的 `heimu` class 可以离线打标，比运行时检测更可靠也更省。

---

## 8. 推荐算法三阶段

**P0 — tag 向量余弦相似度，mean-centered 打分** ✅ 已实现（[src/recommend.py](src/recommend.py)）
纯数值计算，不需要任何模型。

**P0 的天花板已经量出来了，第 3 周动手前先看这段：**

全库平均只有 **3.8 个非零维 / 308 维**（稀疏度 1.24%）。后果是大量作品的
tag 集合**完全相同**，余弦也完全相同 —— 实测某档案的 Top5 是
`0.424 / 0.424 / 0.424 / 0.423 / 0.422`，差在小数点后第三位。
（2026-08-12 起排序已改成 stable，并列时按 subject_id 升序，**结果可复现**；
但「谁该排前面」在 tag 模型里依然没有依据 —— 可复现不等于正确。）
这是「按评分混合排序」的直接动因。

更要命的是特征丢失。**三个可复现的案例，分属两类，对应两种不同的解法：**

**① 区分信息在库里，只是没进 tag 向量** —— 这是 P1 融合 staff/studio 的论据

```
大闹天宫     tag 向量 = [玄幻 0.89, 小说改 0.46]   → 推出《修罗武神》《长生界》等现代网文改
攻壳 S.A.C.  tag 向量 = [科幻, 战斗, 漫画改]        → 推出泛泛的「科幻/原创」作品
```

它们的区分性特征被**正确地**分流掉了（`上美影` 是 STUDIO、`水墨经典` df<8 截断、
`童年` 是 META），推荐逻辑自洽但实质错误。

⚠️ **而区分信息就在库里**：大闹天宫有 `studios=['上海美术电影制片厂']`、
攻壳有 `Production I.G`，覆盖率 92.3%。**这就是 P1 的具体论据** ——
不是「embedding 应该更好」这种泛泛之词，而是一个可复现的失败案例。
第 5 周报告里这比孤立的 NDCG 数字有说服力。

**② 区分信息**根本不在结构化字段里 —— 这是 embedding（文本侧）**独有**的论据
（2026-08-12 全链路 trace 时发现）

档案：`孤独摇滚！=9` / `葬送的芙莉莲=6` / `辉夜大小姐=想尝试` / `BanG Dream! MyGO!!!!!=不感兴趣`

学出来的偏好向量：

```
音乐 +0.507        ← 来自孤独摇滚（打 9 分）
乐队 −0.613        ← 来自 MyGO（不感兴趣）
```

**两部都是乐队番，模型却学出了相反的信号。** 用户真实的区分是
「喜欢孤独摇滚的日常搞笑基调，不喜欢 MyGO 的严肃群像」——
而这个差异**不存在于任何 tag、任何 studio、任何 staff 里**：
两者的题材标签高度重叠，制作公司（CloverWorks / 动画工房）也说明不了基调。

⚠️ **所以案例 ② 是 P1 里 staff/studio 那一半解决不了的**，只能靠简介文本的
embedding。这两类论据要分开讲 —— 混在一起会让「为什么既要结构化特征
又要 embedding」这个设计选择失去说服力。

⚠️ 案例 ② 同时说明 P0 的失败**不都是「召回错东西」**，也包括
「从正确的作品里学出错误的偏好」。后者更隐蔽：推荐结果看上去完全合理
（`大室家` `漫画女孩` `摇曳露营△` 都是对味的日常百合番），
只有回头看偏好向量才发现 `乐队` 被压成了负的。
**第 5 周评测要能捕捉到这类错误，不能只看 Top-K 命中率。**

**③ 区分信息在向量里、也没分流错，但余弦用不上它**
（2026-08-12 线上部署验证时发现）

档案 `死亡笔记=9`，Top3 里出现 **爱探险的朵拉**（学龄前动画，done=155、评分 5.5），
`match=0.808`：

```
死亡笔记      智斗 0.69  悬疑 0.44  推理 0.57  漫画改 0.12
爱探险的朵拉   智斗 0.61  悬疑 0.42  冒险 0.39  原创 0.17  子供向 0.52
```

`智斗`+`悬疑` 高度重合，余弦自然高。**而区分它的标签就在向量里 —— `子供向 0.52`。**
问题是用户没评过任何子供向作品，那一维的偏好权重是 **0**，于是它
**贡献为零而不是负数** —— 余弦没有「否决」这个概念，只有「相似」和「不相似」。

⚠️ 这类失败前面三道防线**全都拦不住**：不是特征丢失（标签在）、
不是分流错误（`子供向` 本就是题材词）、评分下限也够不着（5.5 > 3.5）。

⬜ **可能的解法（等第 5 周 baseline 后再定，别现在动）**：
把少数「资格类」标签（`子供向` `幼儿向` 之类）当成 `nsfw` 那样的**硬过滤维度**，
而不是普通题材维度 —— 用户没主动表达喜欢时默认排除。
⚠️ 但要先量清楚边界：`子供向` 里有哆啦A梦、蜡笔小新这类成人也看的作品，
一刀切会误伤。**这正是第 5 周评测该回答的问题**，现在拍脑袋加规则会污染 baseline。

**P1 — 多语言 embedding 融合 staff/studio 结构化特征**

**P2 — 自适应问卷，用信息增益最大化每题的偏好信号**

### 持久化评分怎么改善预测

注册用户的价值是**评分数在时间上累积**，而不是引入新算法。P0/P1/P2 三个阶段的算法一行不用改，变的只是输入向量更长：

- 游客每次进来都是「答 10 题 → 10 条评分 → 出推荐」
- 注册用户是「上次 10 条 + 这季又打了 8 条 → 18 条 → 更准」

⚠️ **不做全站协同过滤。** 用自家用户的评分建 item-item 共现矩阵需要几百个活跃用户才有信号，作品集项目达不到，硬做只会得到一个噪声矩阵。第 9 节的共现簇**继续用 Bangumi 公开收藏数据**。数据照存（schema 已支持），等真有量了再切。

**顺带的好处：** 有了真实用户评分，可以拿自己的数据当第 5 周离线评测的一个补充验证集——但**主评测协议仍然是 Bangumi 公开数据上的 leave-one-out**，自家数据样本量太小，不能作为主结论。

---

## 9. 聚类选题设计（第 5–6 周）

> 🚫 **核心方案已被实测推翻（2026-08-14）。现行实现是 MMR，不是聚类。**
> 详见 [sql/005_mmr_rank.sql](sql/005_mmr_rank.sql) 与
> [scripts/build_clusters.py](scripts/build_clusters.py)。
>
> **三个诊断的结果**（候选池 4,439 部，embedding 向量）：
>
> | 诊断 | 结果 | 含义 |
> |---|---|---|
> | silhouette | ≈ **0.035**，随 N 单调下降 | 簇间毫无分离，**且定不出 N** |
> | ARI（跨随机种子） | ≈ **0.48** | 换个种子一半簇结构就变 → 不可复现 |
> | PC1 vs 简介长度 | r = +0.053 | ✅ 这一项虚惊，PC1 没有被长度占据 |
>
> 动画简介的 embedding 空间是**连续体**不是分离团块 —— 题材本就渐变。
>
> ⚠️ **但真正的问题是目标搞错了**：本节要的「选 N 部覆盖口味空间」是
> **多样性问题**，不是聚类问题。聚类只是达成它的一种手段，而这手段在这份
> 数据上不成立。MMR（最大边际相关）直接优化目标，且**确定性无随机种子**。
>
> 实测对照（N=30，冗余 = 选中集合两两余弦均值，越低越好）：
>
> | 方法 | 冗余 | 中位热度 |
> |---|---|---|
> | 纯热度（原实现） | 0.4552 | 44,146 |
> | 内容簇 k-means 代表 | 0.4101 | 38,990 |
> | **MMR λ=0.5（现行）** | **0.3781** | **41,040** |
> | *（随机对照）* | *0.3627* | — |
>
> ⚠️ **纯热度选出的题目比随机抽的还冗余 25%** —— 热门作品扎堆在校园/恋爱/日常。
> 实测最冗余的三对：轻音少女×MyGO(0.65)、CLANNAD×春物(0.65)、龙与虎×CLANNAD(0.64)。
> 问 30 题拿不到 30 题的信息量，这就是本节存在的全部理由。
>
> 📌 **k-means 不删**：第 10 节冷启动曲线里「内容簇选题」本就是四条对照线之一，
> `cluster_id` 继续由 build_clusters.py 产出作 **baseline**。
> 「试了聚类 → 量出它不成立 → 换成直接优化多样性」这个过程本身就是第 5 周的材料。
>
> ⚠️ **本节以下的「三个关键设计点」仍然有效** —— 设计点 ① 的
> 「热度权重要够大」正是 MMR 的 λ，② 内容簇 vs 口味簇的区分、
> ③ 与信息增益串联，都不受影响。失效的只是「用 k-means 实现」这一条。
>
> ⚠️ **范围修正**：设计点 ② 说「两个都做当对照实验」，但 dump 里**没有用户
> 收藏数据**（只有 subject/person/character/relations），口味簇需要的
> item-item 共现矩阵得另走 Bangumi API 抓用户收藏 —— 那是独立的数据获取
> 任务。**第 3 周只做内容簇**，共现簇那条线要么排到第 5–6 周要么砍掉。

### 目的
先把 11,259 部聚成 N 个簇，每簇选一部代表作进问卷 → 用最少的题覆盖最大的口味空间。

### 三个关键设计点

**① 簇内选代表 ≠ 取质心最近的**
质心附近往往是小众作，覆盖度好但用户没看过，问了等于白问。应按 `popularity_rank × 到质心距离` 折中打分，**热度权重要够大**。

**② 内容簇 ≠ 口味簇**
对 profile 向量聚类得到的是**内容相似**组（都是校园恋爱、都是机甲）。但推荐要预测的是**口味相似**——题材天差地别的两部作品可能被同一批人喜欢。

用 Bangumi 公开收藏数据建 item-item 共现矩阵再聚类，得到口味空间的簇。**两个都做，当对照实验**。

**③ 与信息增益是串联不是替代**
- 聚类 → 离线算好的候选池（N 部），全体用户共用
- 信息增益 → 在候选池内动态选下一题

顺带解决 IG 的性能问题：候选从 5,000 缩到 N，每轮重算从不可行变成毫秒级。

### 实现细节
- 聚类在 Python 里做，不在 Postgres 里。拉 11,453 条向量（47 MB）→ sklearn
- **先 PCA 降到 30–50 维再 k-means**。高维下距离趋于集中，簇间区分度反而下降
- 簇数 N 从 30 起试，用 silhouette 定
- 问卷必须有「没看过」选项，且要**超发**：要 10 条有效评分就展示 25–30 部

⚠️ **问卷候选池必须再过滤成 TV+WEB，不能直接用全部 11,453 部。**
候选集为了扩大用户可打分范围收了剧场版和 OVA，但这两类大量是**系列续作**（`Fate/stay night [HF] 第三章`、`女神异闻录3 剧场版 第四章`）。问卷抽到这种条目，用户没看过前作就只能选「没看过」，等于白问一题。

所以是**存全量、问卷子集**：库里 11,453 部都参与相似度计算、都支持用户主动搜索打分；但聚类选代表那一步，候选限制在 `meta_tags ∩ {TV, WEB}` 的 **6,537 部**内。

---

## 10. 离线评测（第 5 周，项目核心）

**协议：** Bangumi 公开收藏数据上做 leave-one-out
**指标：** NDCG@10, Precision@10

**四条 baseline：**
1. 随机
2. 热度
3. tag 模型
4. embedding 模型

**冷启动曲线：** 横轴问题数（1→20），纵轴 NDCG@10，四条线：
- 随机选题 / 纯热度选题 / 内容簇选题 / 共现簇 + 信息增益

「答 8 题达到随机选题答 20 题的效果」这类结论，远比孤立的 NDCG 数字有说服力。

**⚠️ 第 5 周不可压缩。这是整个项目最像研究、最能体现严谨性的部分。**

**可选 ablation：** 512 vs 1024 维对检索质量的影响。通常差 1–3%，但中文动画语料需实测。做成「测了维度对质量的影响并选了成本最优点」比报一个数字更能体现工程判断。

---

## 11. 建库分三批

| 批次 | 时间 | 内容 | 量级 |
|---|---|---|---|
| 1 | 第 1–2 周 | `anime_profile`，纯 Bangumi + AniList | **11,453 部**（实测）/ 47 MB |
| 2 | 第 4 周 | 系列剧情文（萌娘百科） | 2,000 部 / ~3 万 chunk |
| 3 | 第 6 周后 | 角色页 | ~8,000 篇 / ~6 万 chunk |

**批次 3 可砍。** 时间不够就不做，不影响主线和评测。

---

## 12. 六周排期

| 周 | 内容 |
|---|---|
| 1 | ✅ Bangumi/AniList 抓取 + schema + 批次 1 建库 |
| 2 | ✅ P0 推荐 + 选题 v0 + 续作折叠 + FastAPI + 打分迁 pgvector ⬜ 前端（评分存 localStorage，接口已是无状态） |
| 3 | Qwen3-Embedding 接入 + P1 融合 staff/studio + PCA/k-means 产出内容簇 |
| 4 | 萌娘百科抓取（批次 2）+ HyDE 检索 + 混合检索 |
| **5** | **离线评测：leave-one-out、NDCG@10/P@10、四条 baseline、共现簇、冷启动曲线** |
| 6 | 信息增益动态选题 + **账号系统（注册/登录/评分持久化/游客转正）** + 部署上线 |

**为什么认证排在第 6 周：** 认证是通用 CRUD，不构成简历差异化，不该挤占 1–5 周的主线。前 5 周全部走游客路径就能跑通推荐、检索、问答和离线评测——**第 5 周的评测用的是 Bangumi 公开数据，和有没有账号系统完全无关**。只要第 2 周把接口做成无状态的，第 6 周加账号就是接一个数据源，不是重写。

---

## 13. 第一步：立即开始做什么

**目标：拿到 11,453 部动画的干净结构化数据并落库。不碰 embedding，不碰萌娘百科，不碰前端。**

### 五个动作

**1. 拉 dump，摸清字段**（半天）
下载解压，`head -n 5 subject.jsonlines`。确认三件事：`type` 怎么区分动画/书籍/游戏、放送日期格式、评分和收藏数在不在里面。**先看清楚再写代码。**

**2. 筛候选集**（✅ 已完成 2026-08-10）

**最终口径（已定）：**
```
type == 2                                    # 动画
AND 有放送年份                                # 无年份下限（见下）
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除「短片」和无形态标签的同人/MV
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

⚠️ **口径唯一事实来源是 [src/candidates.py](src/candidates.py)，不要在脚本里复制筛选逻辑。**

**为什么取消年份下限（2026-08-10）：** 第 7 节的「经典回顾」推荐模式要求库里有老番。狼与香辛料(2008)、灼眼的夏娜(2005) 都在原来的 2011 切线之外，不扩等于空功能。

代价只有 **+4,341 部 / +18 MB**，捞回的却是热度最高的一批作品：

| 年份 | 作品 | done |
|---|---|---|
| 1995 | 新世纪福音战士 | 53,669 |
| 2009 | 轻音少女 | 50,235 |
| 2008 | CLANNAD 〜AFTER STORY〜 | 46,648 |
| 2001 | 千与千寻 | 46,454 |
| 2006 | 死亡笔记 | 40,427 |

EVA 的 done 比任何 2011+ 作品都高。这些经典的热度普遍高于新番，**问卷选题覆盖度反而提升**。

年代分布（2026-08-11 灌库后按库内实际重算）：1989 及以前 **605** / 1990s 862 / 2000–2005 1,189 / 2006–2010 1,491 / 2011+ 7,112，合计 11,259；另有 194 部靠 infobox 补出年份（多为国产老动画），总计 11,453。

⚠️ 此处「1989 及以前」原记 492，是笔误 —— 后四个桶与库内实测分毫不差，唯独这个桶差 113，而原来那 5 个数之和 11,146 也对不上同文写的 11,259。最早的条目是《可怜的比埃洛》(1892，世界第一部动画)，非脏数据。

**`done >= 50` 为什么是好阈值：** 它把三个数据质量问题一次清零——无人看过 0 条、无 tag 0 条、无评分 0 条。有 50 人标记看过的条目必然已积累 tag 和评分，这不是巧合。

**逐层收紧的实测数字**（供以后调口径时参照）：

| 条件 | 数量 |
|---|---|
| type=2 全部 | 30,610 |
| + 有放送年份 | 25,627 |
| + 年份 ≥ 2011 | 15,453 |
| + 形态 ∈ {TV, WEB} | 8,726 |
| + 形态 ∈ {TV, WEB, 剧场版, OVA} | 12,552 |
| **+ done ≥ 50（最终）** | **7,112** |
| 〃 但仅 TV+WEB | 4,689 |
| 〃 但仅日本 | 3,921 |

**不限地区**：保留热门国创（2011+ 中国动画 2,684 条）。地区筛选还有个风险——meta_tags 覆盖率只有 88.7%，没打地区标的会被误杀。

**R18 内容（已定）：** 候选集含 **1,453 部 `nsfw=True`（12.7%）**。策略是**入库保留、默认过滤**：
- 1,453 部照常建 profile、参与相似度计算和共现矩阵 → 第 5 周离线评测的用户行为数据不出现缺口（看里番的用户也看普通番，删掉会丢失关联信号）

⚠️ 原文记的 780 是**取消年份下限之前**（7,112 部口径）的数字，扩到全年份后没更新。实测库内 2011+ 的 nsfw 正好 780，与旧数完全吻合，可佐证只是口径变了。
- `anime_profile` 加 `nsfw` 布尔列，问卷选题和推荐结果**默认 `WHERE NOT nsfw`**，前端给开关

⚠️ **判定必须用 `nsfw` 字段，不能用 tag。** 实测：带 R18 类 tag 的有 925 部，但混进了《圣痕炼金士II》《零度战姬》这类只是有肉番元素的普通 TV 动画（被打「肉」标签）→ 按 tag 过滤会误杀。而 `meta_tags` 里只标了 155 部 → 严重漏标。只有 `nsfw` 字段可靠（该交叉验证在 2011+ 口径下做的：780 部中 777 部与 R18 tag 一致）。

年份分布健康：每年 285–662 部，2016 年后稳定在 550–660，与「每季 50–80 部」的实际产能吻合。

**3. 建表灌数据**（1 天）
`alias` + `anime_profile`。`plot_chunk` 本周不建。向量列建好留空。

**4. AniList 补字段**（1 天）
GraphQL 批量补 staff/studio/英文标题。ID 对齐用 `bangumi-data`。

**5. Tag 清洗**（1–2 天，最重要）

**实测底数（2026-08-10）：** 候选集 11,453 部共 **12,909 个不同 tag**（合并同义词后），每部平均约 11 个。

**① 「保留 300–800 个有效 tag」这个目标不成立（原因和文档设想的不同）**

原文档设想的是「清洗掉噪声后剩 300–800」。实测下来数量确实落在区间内（**418 个**），但**决定数量的是候选集范围，不是清洗力度**：

| 候选集 | KEEP 数 |
|---|---|
| 7,112 部（2011+） | 200 |
| **11,453 部（全年份）** | **418** |

老番带来了一整套新词汇（`赛璐珞` `意识流` `实验动画` `乱马` `圣斗士`），题材 tag 直接翻倍。所以「目标 N 个 tag」这个提法本身没意义——**该问的是「候选集里的题材概念有多少」，答案由数据决定，不由目标决定**。

**采用 `doc_freq >= 8` → 418 个题材 tag。**（`doc_freq` = tag 出现在多少部作品中，非累计投票数）

> 📌 **本节以下的 418 是第一轮清洗的数字，作为过程记录保留。**
> 2026-08-11 第二轮用 dump 的 person 数据自动检测，又剔掉 110 个漏网的
> 人名/IP/公司/繁体形态词，**现行词表是 308 个**。方法见开头「Tag 词表第二轮清洗」。
> 阈值 `df>=8` 没变，变的是分流规则的完整性 —— 这印证了本节下面那句
> 「阈值从来不是关键变量，规则完整性才是」。

⚠️ **阈值从来不是关键变量，规则完整性才是。** 7,112 部口径下，同样 df≥8：规则不全时 KEEP 324 个（混着 124 个漏网人名/公司/IP），补全后 200 个。调阈值的收益远小于补规则。

⚠️ **每次改动候选集口径，必须重跑一遍规则审查。** 扩到全年份后一次性冒出 50+ 个新漏网条目：老一辈监督（宫崎骏、手塚治虫、松本零士）、老 IP（乱马、圣斗士、数码暴龙）、繁体变体（`裡番` `沒看全` `高橋良輔`）、怀旧元评价（`早期优秀作品` `补旧番` `童年回忆`）。**换个年代就是换一套词汇。**

**② 分流结果（不是「保留 vs 丢弃」，是四个去处）**

| 类别 | 个数 | 去处 |
|---|---|---|
| **KEEP** | **418** | → 进 tag 向量，P0 用 |
| STUDIO | 254 | → 分流，作第 4 周 AniList `studios` 的交叉验证 |
| STAFF | 188 | → 分流，作 AniList `staff` 的交叉验证 |
| YEAR | 217 | 丢弃（`air_year` 列已有） |
| IP | 134 | 丢弃（作品名不是口味特征，且让同系列相似度虚高；系列关系走 `subject-relations`） |
| FORM | 50 | 丢弃（`form` 列 + meta_tags 已有） |
| META | 54 | 丢弃（`惨遭动画化` `作画崩坏` `烂尾` `童年回忆` 这类） |
| REGION | 20 | 丢弃（meta_tags 已有） |

**制作公司 254 个 + 人名 188 个 = 442 个，比题材 tag 还多。** 这是 Bangumi tag 系统最大的特点，也是不做分流就直接算余弦会失真的原因：`ufotable` 和 `奇幻` 在向量里权重相同，显然不对。

💡 **手工枚举不是长久之计，第 4 周有正路。**
动作 4 会从 AniList 拉到**结构化的 `studios` 和 `staff` 名单**。拿那份名单去自动标注 tag 里的人名和公司名，比手工维护清单可靠得多，也能覆盖繁体/假名/罗马音的各种变体。**手工清单只是第 1 周的过渡方案**，AniList 数据到位后应该切换过去，手工清单退化为兜底。

⚠️ **`童年` 是怀旧标记不是题材。** 它在 2011+ 口径下 df=257，全年份口径下暴涨到 **971**——因为老番几乎人人打。放进 tag 向量会让所有老番互相相似，直接污染「经典回顾」模式的推荐质量。同类的还有 `童年回忆` `儿时回忆` `早期优秀作品` `补旧番` `旧物`。

⚠️ **分流 ≠ 丢弃。** 喜欢京阿尼、喜欢泽野弘之配乐都是真实口味维度，但它们的正确归宿是第 8 节 P1 的「staff/studio **结构化特征**」（走 AniList，比用户 tag 完整可靠），不是混进 P0 的 tag 余弦。

**③ 两条启发式规则，避免随阈值下降无止境枚举公司名**

- **纯 ASCII → 判为 STUDIO**，白名单放行 13 个题材缩写（`NTR/R18/BL/JK/SF/nur/chippai/LOLI/SM/TS/GL/NL`）。实测 df≥5 的 88 个纯 ASCII tag 里只有 9 个是题材，其余全是公司/IP
- **纯假名 → 判为 STUDIO**（`ショーテン` `バニラ` `ティーレックス` `ばにぃうぉ～か～`）

规则表在 [src/tag_rules.py](src/tag_rules.py)，词表产物在 `data/interim/tag_vocab.json`。

**② 必须合并的同义词（按影响从大到小）**

```
漫画改 (2,534) + 漫改 (1,679)        ← 合计覆盖 59% 候选集，最大的一对
轻小说改 (754) + 轻改 (437)
剧场版 (1,265) + 动画电影 (318) + 电影
国产 (1,004) + 中国 (692) + 国漫 (357) + 国创
```
⚠️ 文档原先举的例子是「百合/GL」，但实测最大的同义对是**漫画改/漫改**。不合并等于把同一概念拆成两个维度，tag 向量余弦会失真，**直接压低 P0 上限**。

⚠️ `小说改`(511) 与 `轻小说改` **不是同义词**（一般小说 vs 轻小说），别合并。

**③ 要剔除的噪声只有两类，比预想的少**

- **年份 83 个**（`2011`…`2026`，每年 400+ 部被打标）← 最大噪声源
- **形态/地区 16 个**（TV / OVA / 剧场版 / WEB / 短片 / 国产 / 日本 / 欧美…）← meta_tags 已有结构化版本，用户 tag 版本纯属重复

**④ 必须合并的同义词（实际生效清单）**

```
漫画改(3,789) ← 漫改              最大的一对，覆盖 34% 候选集
游戏改(1,328) ← GAL改, 手游改, 游戏改编, 游改
里番(1,546)   ← 2D里番, 里, 裡番(繁), 旧里番
轻小说改(879) ← 轻改
步兵(361)     ← 步兵里番, 步兵裡番
泡面番(908)   ← 泡面        治愈(751) ← 治愈系      萌(231) ← 萌系
18禁(480)     ← 18X, H      卖肉(334) ← 肉, 肉番    运动(266) ← 体育
无码(471)     ← 無修正      重口(92)  ← 重口味      腐向(98)  ← 腐
耽美(142)     ← 耽美系      少女向(83) ← 少女系
```

⚠️ **繁简变体是取消年份下限后才暴露的一类问题**（`裡番`/`里番`、`沒看全`/`没看全`、`高橋良輔`/`高桥良辅`）。老条目的编辑者用繁体的比例明显更高，加规则时两种写法都要覆盖。

⚠️ **明确不合并的易混对**（写下来防手贱）：
`小说改` ≠ `轻小说改`（一般小说 vs 轻小说，受众差别大）· `百合` ≠ `轻百合` · `后宫` ≠ `逆后宫` · `萝莉` ≠ `幼女`

**⑤ 两个容易踩的坑**

- ⚠️ **`治愈` / `催泪` / `致郁` 是题材不是元评价，必须保留**（《夏目友人帐》那一类）。用正则识别元评价时最容易误删这三个
- **文档原先预设要对付的「神作」「补番」「力荐」，在高频区一个都没出现。** Bangumi 用户 tag 比预想干净，真正的噪声是结构化信息（公司/人名/年份/IP）被塞进了 tag，不是情绪化评价

**⑥ R18 细分 tag 全部保留**

`里番` `NTR` `触手` `人妻` `调教` `凌辱` 等是真实题材特征。既然 1,453 部 nsfw 作品入库保留（见第 13 节动作 2），nsfw 用户群体内部的偏好差异同样需要 tag 刻画，删了等于把这 1,453 部的特征抹平成一片。

**这一步质量直接决定 P0 上限，别赶。**

### 验收标准

```sql
SELECT name_cn, tags, air_date, score
FROM anime_profile
WHERE air_date >= '2011-01-01'
ORDER BY score DESC LIMIT 20;
```

返回 20 部名字正确、tag 干净、无乱码无空值的动画 → 第一步完成。

### 执行建议

**先跑 50 部端到端，再全量。** 从 dump 挑 50 条走完整流程灌库，人工看一遍。字段理解错了，这时候改成本是几分钟；全量跑完再发现是几小时。

---

## 14. 待确认事项

- [ ] 本地 vs API embedding 一致性验证（建库前必做）
- [x] ~~dump 里是否含评分/收藏数~~ → **含**，`score`/`score_details`/`rank`/`favorite` 齐全，不需额外调 API
- [x] ~~真实候选集规模~~ → **11,453 部**（全年份）。灌库后实测占用 **44 MB**，加上未来的 vec 与 plot_chunk 总预算 ~344 MB / 余量 31%（vec 改 halfvec 则 ~320 MB / 36%），明细见第 5 节
- [ ] 萌娘百科角色页密度（影响是否触发拆库方案）
- [ ] 512 vs 1024 维 ablation 结果
- [ ] JWT 放 httpOnly cookie 还是 Authorization header——前端在 Vercel、后端在 Render 属**跨站**，cookie 需要 `SameSite=None; Secure` 且要配 CORS `credentials`，第 6 周实测再定
- [ ] 游客 localStorage 评分的过期策略（一直留着还是 N 天清理）
- [ ] **Phase 2 阻塞项**：`ingest_raw` + `rating_snapshot` 会超 Neon 0.5 GB（第 5 节修正后余量约 156 MB，vec 用 halfvec 则约 180 MB）。三条出路见 [docs/phase2-overseas-data.md](docs/phase2-overseas-data.md) 第 0 节，**动手前必须先选一条**

---

## 15. 贯穿始终的原则

1. **离线评测是简历差异化的核心**，不是可选项，不可压缩
2. **检索前剧透门控 > 运行时 LLM 检测**
3. **两种索引粒度必须分离** — 混用会同时拖垮发现和问答
4. **实体消歧必须限定作品范围**
5. **先跑通小样本再全量** — 适用于每一个数据处理环节
6. **每个阶段都要有能跑的东西** — 不要为了完美的架构推迟第一个可用版本
7. **推荐链路对「评分从哪来」无感知** — 游客和注册用户共用一条代码路径，账号只是数据来源之一
8. **登录不是使用门槛** — 游客能用全部功能，注册只换来「记住我的评分」这一件事


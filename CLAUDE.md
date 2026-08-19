# 动画推荐系统 — 项目前情提要

> 本文档为项目决策记录与执行计划，供 Claude Code 作为初始上下文引入。
> 所有技术选型均已论证完毕，**除非有新证据，不要重新讨论已决事项**。

---

## 🧭 怎么读这份文档（2026-08-19 拆分后的结构）

CLAUDE.md 只保留**每次开工需要的**：现状、操作手册（A–D）、待办。
归档性内容（论证过程、实测记录、原始设计稿）已**逐字**拆到 docs/，冲突时按时效判断：

| 在哪 | 内容 | 时效 |
|---|---|---|
| 本文 · 第一部分 | 现状与下一步 | 最新，每次干完活就更新 |
| 本文 · 第二部分 A–D | 模型 / 部署 / 脚本 / 环境（操作手册） | 最新，**实操以这里为准** |
| 本文 · 第三部分 | 已完工论证的**结论速查**（勿回退） | 已定案 |
| 本文 · 第四部分 | 待办 | 活的清单 |
| [docs/corpus.md](docs/corpus.md) | E / F / H 节：萌娘语料 · plot_chunk · 语料转中文 | 已完工的实测记录 |
| [docs/retrieval.md](docs/retrieval.md) | G / I 节：检索层设计与实现 | 已完工，第 5 周评测的主要依据 |
| [docs/history.md](docs/history.md) | 第三部分详情 + 第 3 / 4 周动作清单 | 已定案，论证过程 |
| [docs/design-doc.md](docs/design-doc.md) | 第五部分：设计文档 第 1–15 节 | **最初设计稿**，部分结论已被实测推翻（原处有标注） |
| [docs/phase2-overseas-data.md](docs/phase2-overseas-data.md) | Phase 2 海外数据源（独立的 1–9 节编号） | 六周排期内不实现 |

⚠️ **交叉引用约定（全文几十处「见 X.Y」未逐一改写，按此定位）**：
「A.x–D」在本文第二部分；「E.x / F.x / H.x」→ docs/corpus.md；
「G.x / I.x」→ docs/retrieval.md；「第 N 节」(N=1–15) → docs/design-doc.md；
「第三部分」的详情与「第 3/4 周动作清单」→ docs/history.md。
**所有编号（A–I、第 1–15 节）是稳定锚点，拆分后依然不要重新编号。**
新增内容一律写进本文一/二/四部分或对应 docs 文件，不要往 design-doc.md 里插。

---

# 第一部分 · 现状与下一步

## 📍 当前进度（更新于 2026-08-19）

**阶段 05 检索层 ①②③④ 已实现并上线端点。下一步：第 5 周离线评测。**
实现细节与这一轮抓到的 bug 全部记在 **I 节**；G 节保留为设计依据。

```
POST /api/ask       流程 C 剧情问答，四步管道         src/retrieve.py
GET  /api/related   同作者/导演/公司的其他作品，零模型   src/related.py
```

⚠️ **实现过程推翻了 G 节的两条结论**，都是实测逼出来的（详见 I.2）：
① **G.6 末尾「最终排序全权交给 rerank」只在没有 ① 直取时成立** ——
实测「冈部有什么能力」把他本人的 chunk 挤出前 8，而排第 1 的正是 G.5f 里
让 Hunyuan 张冠李戴的那条菲利斯 ⇒ 加 `PIN_RESERVE` 保底席位。
② **rerank 的分数不能只用来排序** —— 低分 chunk 会稀释上下文把 LLM 逼成拒答，
实测同一题 8 条→拒答、3 条→答对 ⇒ 加 `MIN_SCORE` 相关度地板。
📌 ⑤ HyDE 仍未实现，按 G.5d 默认关。

⬜ 悬着一件事：**路径③ 跨作品检索 579 ms**，要不要建 HNSW —— 见 F.1 的标注。
📌 **现在多了一条 CU 论据**：路径② 是索引查找（0.1 ms），路径③ 是
`Parallel Seq Scan` 扫 89,544 条向量 / 176 MB TOAST，**541 ms 全是真实 CPU**。
而今天做的 `/api/related` 恰好用一条走索引的 SQL 替掉了它的一大类用途（I.3）。

### ▶️ 下次开工从这里接（2026-08-19 收工）

**先做 `GET /api/season`**（一条 SQL，复用第 7 节的季度窗口口径，
「十年前的这个季度在播什么」立刻能答）—— 它是单一入口路由的第 1 步，
完整设计见第四部分「单一入口 vs 功能按钮」。之后：流程 B 找番 → 合并进
/ask 加 route 字段。**阶段 05 主线已通，第 5 周评测是下一个不可压缩的主线。**

⬜ **同 IP 衍生折叠** —— `/api/related` 实测 **32% 的结果是被问作品自己的衍生品**
（问灵能百分百返回「REIGEN」「第一回灵能相谈所」「10周年纪念映像」）。
根因是 `series_root` 只折叠 `rt=2/3`（前传/续集），没折叠 `rt=6/11/12`（番外/衍生）——
正是第三部分那条挂着的「⬜ 遗留缺口：`rt=12` 没有折叠」。
两个修法与取舍见 **I.3 末尾**。

⬜ **HyDE 的正式对照**（10–20 条查询），判定该不该默认开（G.5d）。
⬜ **重测跨语言惩罚**：日文已降到 0.36%，原先那个 −27.7pp 重跑一次就是
第 5 周报告的现成材料。
⬜ **`/api/ask` 的 prompt 若要改，先把它纳入 `llm.descriptor()` 的 fingerprint** ——
现在指纹只覆盖 provider/model/temperature，**改 prompt 指纹一个字符都不变**，
第 5 周评测日志会声称两批数字同源而实际不是。同一条纪律 `embed` 和
`translate_cache`（`PROMPT_VERSION`）都做对了，唯独 LLM 这条漏了。

**未提交**（Kevin 自己提交）：`CLAUDE.md`
—— 阶段 05 那批已在 `3e29085` 及其后的提交里入库。

⚠️ **备份提醒**：`data/interim/translate_cache/`（50 MB）是 4.4 万条译文的**唯一副本**
（H.5 已否决入库），丢了是重翻 8 小时。

第 3 周产出：Qwen3-Embedding 建库（10,864 部）· 问卷选题改 MMR 多样性序 ·
问卷支持多次作答 · P1 三路融合（tag + embedding + staff/studio）。

`animetion-recommender.vercel.app` —— 前端 + API 同一个 Vercel 项目、同源。
库占用 **770 MB**（`pg_database_size()`；Neon 控制台口径更高，见第 5 节）。
`plot_chunk` **89,544 条**（萌娘 20,127 + dump 角色 69,417）· 覆盖系列根 6,131 个 ·
**覆盖作品 79.3% · 热度加权 97.3%**。
`alias` **235,047 行**（subject 38,378 + character 196,669）。
测试 **52 项**（18 项打分一致性 + 16 项接口 + 18 项检索层纯函数）。

📌 **2026-08-18：语料已全部转为中文** —— 作品 summary 日文残留 **39 / 10,864 (0.36%)**、
角色 chunk **560 / 69,417 (0.81%)**。做法见 **H 节**，灌库过程见 **H.7**。
⚠️ 上面那几个数是灌库后的现值；F.7 / G.1 里的 `69,999` `90,126` `636 MB`
是**阶段 03 当时的历史记录**，不要拿来当现状用。

| 周 | 内容 | 状态 |
|---|---|---|
| 1 | 数据层：dump → 候选集 → 灌库 → tag 清洗 | ✅ |
| 2 | P0 推荐 + 选题 + 续作折叠 + API + pgvector + 前端 v0 + 部署 | ✅ |
| 3 | Embedding 建库 ✅ · 问卷选题多样化(MMR) ✅ · P1 融合 staff/studio ✅ | ✅ |
| **4** | 萌娘语料 ✅ · 角色语料(03) ✅ · 语料转中文 ✅ · LLM 选型 ✅ · 检索层(05) ✅ | ✅ |
| **5** | **离线评测（核心卖点，不可压缩）** | ⬜ **← 下一步在这** |
| 6 | 信息增益选题 + 账号系统 + 季度同步 | ⬜ |

### 2026-08-16 这一天做完的（详见 F 节）

```
阶段 01  sql/007 建三张表（正文进库 · 不建 HNSW · halfvec(1024)）
阶段 02  灌 19,526 chunk  ≤¥0.28
阶段 04a 标题补救 → +601 chunk、+229 条映射
         覆盖作品 36.1% → 40.7% · 热度加权 78.8% → 83.3%
前置     embedding 并发 8 路，实测 1.44 → 0.29 s/批（4.9×）
```

### 六阶段计划（2026-08-16 定，编号即依赖顺序）

| 阶段 | 内容 | 状态 | 规模 / 成本 |
|---|---|---|---|
| **01** | `sql/007` 建三张表 | ✅ | 三表五索引，幂等 + 五项负向测试 |
| **02** | 灌萌娘作品页 chunk | ✅ | 19,526 条 / 602 批 8 路 / ≤¥0.28 |
| **03** | 灌 dump 角色简介 | ✅ **2026-08-16** | **69,999 chunk** / 66,748 角色 / ¥0.65 · 详见 **F.7** |
| **04a** | 标题补救 | ✅ | +601 chunk · +229 映射 / ¥0.009 |
| **04b** | 角色页链接提取 | ⬜ | 从已抓的 2,301 页 HTML 提链接，**算准阶段 06 体积** |
| **05** | 检索层（流程 C） | ✅ **2026-08-19** | 设计见 **G 节**，实现与实测见 **I 节**。流程 B（找番）未做 |
| **06** | 抓萌娘角色页 | ⬜ 可选 | ≈22,814 页 / **15–40 小时** / 编码 ¥0.3。📌 理由已累积三条：更详细 · 中文角色语料唯一来源 · **结局等剧情细节住在角色页**（GOSICK 实例，I.9） |

📌 **顺序建议：03 → 05。** 03 只要一个脚本、零抓取、¥0.46，而它补的是
**角色那一层两级全空**（既无 dump 简介也无萌娘页）—— 这才是角色问答完全答不了的
根因，不是"没抓萌娘角色页"。且 05 的分层配额要靠 03 的数据才能标定。

⚠️ **阶段 03 顺手把 `alias` 的角色行填了。** 实测那张表 **38,378 行全是
`entity_type='subject'`，角色行一条没有** —— 而第 1 周就为它留好了
`parent_subject_id` 列（注释写着「角色消歧必须锚定在已确认的 subject 范围内」）。
同一份 `character.jsonlines` + `subject-characters`，零额外成本。
⚠️ 没有它，「吉尔伽美什最后怎样」路由不到角色，而**主角重名 6.4%**
（アリス×9、主人公×9）在没有作品锚定时无解。
验收：`character_id` 全非空 · scope 覆盖 9,245 部 · `alias` 出现 character 行。

⚠️ **阶段 06 的体积可以在抓取之前算准，不用估。** `prop=info` 一次给 50 个标题的
`length`（wiki 源码字节数），而这是标题解析本来就要做的一步。作品页实测换算率
**0.469 chunk/KB**，但它随页面变长单调下降（0.889 → 0.391）—— 因为长页面的长度
来自表格和列表，**而那些我们本来就丢掉**（火影 175 KB 只出 11 chunk，银魂 144 KB → 13）。
⚠️ 这条对角色页**不能直接套**：角色页的长度来自「个人经历」这类散文，是要留的，
换算率大概率接近 0.889 那一档。⇒ 抽 30 页真抓一次校准再乘全量 length，
40 小时值不值就变成一道算术题。

💡 **角色数是极端长尾，天然有个控制阀**：每系列中位 9 · p90 32 · **max 801（航海王）**，
光之美少女 702 · 宝可梦 389 · 火影 348。**前 1% 的系列吃掉 11.8% 的角色。**
按系列设上限（比如 30）只影响 10% 的系列，却能把航海王从 801 砍到 30。

📌 存储：三阶段全做完 ≈131,000 chunk · 向量 269 MB · 正文 64 MB · **$0.12/月**。
存储不再是约束，这是敢把批次 3 排进来的前提。

📌 原始的六阶段 artifact（含更详细的推导过程）：
[plot_chunk 施工计划](https://claude.ai/code/artifact/a69a6f1a-22b3-40c6-b3e2-6a01bdb7bf3a)。
⚠️ 它里面「未提交的改动：embed.py 并发改造」一条**已过期**（已随 bba4435 / 9d1d8f4 入库），
且**剧透门控那条的归因是错的** —— 见 F.4 ③ 的更正。

✅ **带前端的部署配置已实测通过（2026-08-15）** —— 此前这里挂着「唯一未经实测的
环节」，现已消除。`buildCommand` + `outputDirectory` + 只转发 `/api/*` 这套组合
线上验证：根路径返回 Vite 构建产物（`/assets/index-*.js` 带 hash），
`/api/health` 返回 `catalog_size=11453 · with_tag_vec=11311 ·
dict_fingerprint=6a1cbbe1bc4f446d`。根目录没有 `package.json` 并未让 Vercel 的
install 阶段报错，**不需要把 Install Command 设成空**。

✅ **已补（2026-08-19）：`/health` 现在报五个覆盖率字段。**
`with_tag_vec` · `with_embed_vec` · `with_staff_vec` · `plot_chunks` ·
`plot_chunks_with_vec`。
⚠️ 补它的理由不是整齐：P1 之后 match 是三路融合，**漏跑
`build_embeddings.py` 或 `build_staff_vectors.py` 时那两路会整项跳过**
（零向量参与融合会稀释另外两路，代码是有意跳过的），结果依然像模像样，
只是悄悄退化成了纯 tag 模型。plot_chunk 那两个则是 `/api/ask` 的前置 ——
没有 vec 会被报成「这部作品没有语料」，症状和忘了灌库一样。


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

【第 4 周 · 🔄 进行中】
   萌娘百科 ──fetch_moegirl.py──► data/raw/moegirl/*.html.gz   只抓不解析
             ──parse_moegirl.py─► moegirl_chunks.jsonl        kind=prose/songs
                                  ⚠️ 两阶段分开，切分策略改了不用重抓（E 节）
             ──[Embedding]──────► plot_chunk        ⬜ 待 sql/007
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
| `etl` | bgm-tv-wiki / tqdm / **lxml** | ❌ `scripts/` 专用。lxml 只用来解析萌娘百科的 Parsoid HTML（E.7），线上不碰 HTML |
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
- 🚨 **连接池必须配 `check=ConnectionPool.check_connection`**（2026-08-19 修，
  **第 2 周就埋下的 bug，影响所有端点**）。Neon 是 serverless，空闲连接会被它回收，
  而 psycopg_pool 自己不知道 —— 下一个请求拿到那条死连接就炸：

  ```
  psycopg.OperationalError: SSL connection has been closed unexpectedly
  ```

  实测复现：开着服务打开 `/api/docs` 看几分钟，第一次 `POST /api/ask` 直接 500，
  紧接着重试就 200。**线上少见只因为流量近乎为零、几乎每个请求都落在新容器上**；
  真正的触发条件是「容器还活着但连接已被回收」，也就是两次访问间隔超过空闲窗口
  —— 正是「打开文档看一会儿再点 Try it out」这个节奏。
  ⚠️ 顺带把 `max_idle` 定为 180 s：**psycopg_pool 的默认值是 600 s，
  比 Neon 的空闲窗口还长**，本身就是隐患。
  ⚠️ 代价是每次借出多一次往返（一条空查询），同区实测约 15 ms —— 换掉一个 500 值得。

  📌 **验证方式很重要，见 I.8 ①**：`pg_terminate_backend` **复现不出**这个故障
  （它发 RST，socket 层立刻可见），必须真晾满 600 秒。对照实验结果：
  不带 `check` → `OperationalError`；带 `check` → 200 / 2.66 s
  （**这 2.66 s 含 Neon compute 从缩容态唤醒，正好回答了第四部分那条
  「⬜ 先量真实冷启动延迟再决定要不要保活」—— 为它每月付 $37.8 显然不值**）。
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
| [scripts/fetch_moegirl.py](scripts/fetch_moegirl.py) | 抓萌娘百科 → `data/raw/moegirl/*.html.gz`（**只抓不解析**）。幂等、可断点续跑。⚠️ 7 秒/请求，全量约 3.9 小时，见 E.1 |
| [scripts/parse_moegirl.py](scripts/parse_moegirl.py) | 解析上一步的 HTML → `data/interim/moegirl_chunks.jsonl`。**不写数据库** —— 切分粒度要由它的实际产出来定，见 E.4 |
| [scripts/build_plot_chunks.py](scripts/build_plot_chunks.py) | `moegirl_page` / `plot_chunk` / `plot_chunk_scope` 三张表 + `build_meta['plot_chunk']`。幂等，靠 md5 比对跳过未变化的行（F.4 ①） |
| [scripts/rescue_moegirl_titles.py](scripts/rescue_moegirl_titles.py) | 补救标题解析漏掉的系列。⚠️ **三步走**：解析（联网）→ 人工过一眼 → `--from-file --apply-b` 应用。中间那步不能省，见 F.3 |
| [scripts/build_char_chunks.py](scripts/build_char_chunks.py) | `plot_chunk` 里 `source='bangumi_char'` 的行 + `plot_chunk_scope` + **`alias` 里 `entity_type='character'` 的行** + `build_meta['char_chunk']`。⚠️ 与 `load_profiles.py` 用**不同的 alias source 值**（`char_*` 前缀），两者永不相交。幂等，md5 跳过 |
| [scripts/translate_corpus.py](scripts/translate_corpus.py) | **只写本地缓存 `data/interim/translate_cache/`，不碰数据库**。⚠️ 与灌库分家的理由：翻译很贵（数小时），而灌库策略可能改几次 —— 分开就不用为了改灌库重翻一遍。可中断续跑，自动防系统睡眠 |

📌 **阶段 05 新增三个模块，都在 `src/` 且都不写库**（线上请求路径要用，
而 `.vercelignore` 排掉了 `scripts/`）：

| 文件 | 职责 |
|---|---|
| [src/retrieve.py](src/retrieve.py) | ①②③④ 主管道 + G.4 状态机。**只读库** |
| [src/rerank.py](src/rerank.py) | `BAAI/bge-reranker-v2-m3` 客户端。⚠️ **可以换模型**（输出是相对排序，用完即弃），A.8 那条铁律不适用于它 |
| [src/related.py](src/related.py) | 结构化关联查询，读 `staff` / `studios` 两列。**零模型调用** |

⚠️ **两个 loader 现在会调 `src/langclean.py` + `src/translate_store.py`**
（顺序：剥离 → 换译文 → 切块，见 H.4）。它们是**纯函数 + 本地缓存查询**，
不写库、不联网，所以不改变上表的「各管各的列」边界。

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

# ── 第 4 周语料层 ──────────────────────────────────────────
psql < sql/007_plot_chunk.sql                # plot_chunk 三张表
uv run --group etl python scripts/fetch_moegirl.py    # ⚠️ 约 4 小时（7 秒/请求，别调低）
uv run --group etl python scripts/parse_moegirl.py    # 纯本地
uv run --group etl python scripts/build_plot_chunks.py  # ≈5 分钟 / ¥0.28
#   ⚠️ 换机器后 moegirl_titles.json 不入 git，标题解析会重跑，
#      而 rescue 的三条候选规则不在 fetch_moegirl 里 —— 见 F.5
uv run --group etl python scripts/rescue_moegirl_titles.py              # 解析，只报告
uv run --group etl python scripts/rescue_moegirl_titles.py --merge-titles
uv run --group etl python scripts/fetch_moegirl.py --reuse-titles       # 只抓新条目
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/rescue_moegirl_titles.py --from-file --apply-b

# ── 阶段 03：角色语料（零抓取，只读 dump）──────────────────
uv run --group etl python scripts/build_char_chunks.py --limit 500   # 先小样本
uv run --group etl python scripts/build_char_chunks.py               # ≈15 分 / ¥0.45

# ── 语料语言统一（H 节）────────────────────────────────────
psql < sql/008_translation.sql               # 译文备份表
#   ⚠️ 换机器第一步：从库里恢复译文缓存，否则 loader 查不到译文、
#      会把日文原样写回去（且不报错）
uv run --group etl python -c "from src import db,translate_store; \
    print(translate_store.restore_from_db(db.connect()))"
uv run --group etl python scripts/translate_corpus.py --scope profile  # ≈40 分
uv run --group etl python scripts/translate_corpus.py --scope char     # ≈3.5 小时
#   ⚠️ 译完要重跑 load_profiles / build_char_chunks 才会把译文灌进库，
#      并重新编码（文本变了 → 向量必须跟着变）

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

## E. 萌娘百科语料链路 → [docs/corpus.md](docs/corpus.md)

> ✅ 已完工。铁律速查：**7 秒/请求勿调低** · UA 诚实（不冒用被封禁 bot 名） ·
> robots 的 `ai-train=no` ⇒ **这份语料永不可用于训练/微调**（embedding 编码不算训练） ·
> CC BY-NC-SA：公开展示正文必须署名 · 抓取与解析两阶段分离（改切分策略不用重抓）。

## F. plot_chunk 语料层 → [docs/corpus.md](docs/corpus.md)

> ✅ 已完工。速查：正文进 Neon（推翻原设计） · **未建 HNSW**，路径③ 579 ms 悬而未决（见 I.5 与 F.1 标注） ·
> 灌库靠 md5 跳过未变行（否则全表 UPDATE 让 TOAST 翻倍） ·
> **F.5 换机器陷阱**：`moegirl_titles.json` 不入 git，rescue 三条规则要补跑 ·
> `plot_chunk` 的 176 MB 向量在 TOAST 里，要用 `pg_total_relation_size` 看。

## G. 检索层设计（阶段 05）→ [docs/retrieval.md](docs/retrieval.md)

> ✅ 设计依据（实现见 I 节；G.6 两条结论已被 I.2 推翻）。速查：
> LLM `PRIMARY = Qwen/Qwen3-14B` · `FALLBACKS = (GLM-4.5-Air,)`——**fallback 按问答质量选，不按延迟**（G.5f） ·
> HyDE **默认关**（G.5d，语料转中文后价值基本抵消） · 召回 50 → rerank → 前 8（G.6） ·
> **rerank 可以换模型，embedding 绝对不行**（输出是相对排序、用完即弃） ·
> 点名查询走 alias 直取，不靠加大 k（G.5g）。

## H. 语料语言统一 → [docs/corpus.md](docs/corpus.md)

> ✅ 已完工（日文残留 0.36% / 0.81%）。速查：管道顺序**剥离 → 换译文 → 切块**，
> 译文缓存的键是剥离后文本（顺序反了 100% 未命中） ·
> ⚠️ `data/interim/translate_cache/`（50 MB）是 4.4 万条译文的**唯一副本**，丢了重翻 8 小时 ·
> 凡改变切块结果的改动都要清孤儿行（判定必须复用 `load_corpus()`，不能另写推导）。

## I. 检索层实现（阶段 05）→ [docs/retrieval.md](docs/retrieval.md)

> ✅ 已完工（`/api/ask` · `/api/related`）。速查：推翻 G 节两条 ——
> `PIN_RESERVE` 保底席位（alias 直取的 chunk 不能被 rerank 挤掉）+ `MIN_SCORE` 相关度地板
> （低分 chunk 稀释上下文会把 LLM 逼成拒答；pinned 豁免地板）（I.2） ·
> G.4 四种状态一律返回 200，失败按层分开：embedding 挂 503 / rerank 挂降级 200 / LLM 挂 503（I.6） ·
> 请求路径要传短重试预算，离线参数不能直接搬（I.4，实测放大到 883 秒） ·
> **「覆盖率」≠「可回答率」，第 5 周评测必须分开报**（I.9，GOSICK 案例）。

---

# 第三部分 · 已完工的论证（勿回退）→ [docs/history.md](docs/history.md)

> 详情（含实测数据与第 3/4 周动作清单）已移至 docs/history.md。
> 这些都已定案并附实测依据，**除非有新证据，不要重新讨论**。结论速查：

- **候选集 11,453 部**，口径唯一事实来源 [src/candidates.py](src/candidates.py)；`done>=50` 是好阈值（一次清零三个数据质量问题）
- **Tag 词表 308 个**（第二轮用 dump person 数据自动检测人名/公司；共现 + 名字相似两层缺一不可）
- **推荐结果评分下限 `MIN_SCORE=3.5`**：判据是原始均分不是 wr；未评分作品放行（upcoming 档依赖这一点）
- **问卷与推荐都折叠续作**：判据 `rt=2`（前传）+ 播出不晚于本作；⬜ `rt=12` 未折叠 → 见第四部分「同 IP 衍生折叠」
- **meta_tags 兜底效果有限**：142 部零 tag 向量，embedding 已救回 131 部（92%）
- **staff/studio 走 dump 不走 AniList**（覆盖 92% vs 56%）；AniList 真正独有的只剩 `idMal` 和全球热度
- **`search_tsv` 不含 summary**（占全库 33%；剧情检索走向量那条腿）
- 📌 **前端刻意冻结在 v0**：先把逻辑全部做完，最后一次性写前端。**不要顺手美化它、不要顺手接新接口。**
  前端欠账清单（搜索/详情页/多次作答/experience/mode）在 docs/history.md 第 2 周一节里。

---

# 第四部分 · 待办（按优先级）

### ✅ 已做：自动扩容上限压到 0.5 CU（2026-08-15）

升级付费后 compute 是 **$0.105 / CU-小时**，而**存储全都要也才 $0.16/月** ——
成本结构整个反过来了，现在唯一值得管的是 compute。已设为 **min 0.25 / max 0.5 CU**：

| 配置 | 常驻上限 | 5 次访问/天 | 50 次/天 |
|---|---|---|---|
| 16 CU（原上限） | $1,210 | $33.6 | $336 |
| **0.5 CU（现在）** | **$37.8** | **$1.05** | $10.5 |

**最坏情况降了 32 倍**，而 0.5 CU ≈ 2 GB RAM、整个库才 110 MB，日常查询不受限
（1.1 万行暴力余弦实测 ≈ 0 ms）。

⚠️ **改设置时有个坑：面板上「Change default compute settings」那个对话框自己写了**
> *Modifying these defaults does not alter the settings of any existing computes.*

**它设的是新建 compute 的默认值，现有那个不受影响。** 要真正生效得去
Branches → 对应 compute → Edit 单独改。⬜ **确认一下现有 compute 的 max CU 实际是多少**，
别停在「我改过了」。

⚠️ **绝不加保活探针**，间隔 ≤5 分钟就等于永不缩容。
这条与「放弃 Render 是因为冷启动 50 秒」的直觉冲突 —— 但那是**应用进程**冷启动，
Neon 缩到零唤醒的只是数据库 compute。
✅ **已量（2026-08-19）：晾满 600 秒后第一次查询 2.66 秒**（含 compute 唤醒）。
⇒ **为它每月付 $37.8 显然不值，保活探针这件事就此结案。**
数据来自连接池 `check` 的对照实验，见 B 节与 I.8 ①。
详见第 5 节「已升级付费计划」。

⚠️ **一个例外：第 4 周灌 chunk + 建 HNSW 索引时要临时把上限调回去。**
0.5 CU 建 3 万条向量的 HNSW 会很慢。
💡 而且**这么做几乎不额外花钱** —— 按 CU-小时计费，同样的活儿
「4 小时 × 0.5 CU」和「1 小时 × 2 CU」都是 2 CU-小时，**成本持平但墙钟时间少 4 倍**。
⚠️ 建完记得调回 0.5，否则常驻上限又回到高位。

### ⬜ 🧭 单一入口 vs 功能按钮 —— 待定的产品架构决策（2026-08-19 Kevin 提出）

**Kevin 的倾向：集成到一个端口，用户用起来方便。**
他同时指出：「关于时间的问题、或者没有完全归类到某一部动画的问题，
**也应该能由语义问答来回答**」。

两个选项：

| | 做法 | 代价 |
|---|---|---|
| A 功能按钮 | 新番推荐 / 老番推荐 / 动画资讯 / 剧情问答 各一个入口 | 用户要先知道自己的问题属于哪一类；四个入口四套 UI |
| **B 单一入口**（Kevin 倾向） | 一个输入框，服务端分派 | 需要**意图路由**，而路由错了是静默的 |

#### 📌 关键：G.4 其实已经为 B 埋好了一半

> 「⚠️ **模糊描述的消歧必须复用流程 B，不要另写一套。**
> 『讲机器人的那个番』正是 `anime_profile.vec` + BM25 要解决的问题……
> 另建一套等于两套口径，第 5 周评测时会打架。」

**这条已经承诺了「认不出具体作品时落到流程 B」** —— 也就是 B 方案的兜底分支。
⇒ 单一入口不是新增架构，是把已经定下的东西接起来。

#### ⚠️ 但要区分「意图路由」和「意图分类器」

第 15 节原则 2（检索前门控 > 运行时 LLM 检测）反对的是**让模型判**。
而这里的分派**可以完全用已有的确定性信号**，不需要分类器：

```
① resolve() 认出具体作品/角色  → 流程 C 剧情问答      已实现（state=ok）
② related.wants() 命中岗位关键词 → 结构化关联查询      已实现
③ 问句含时间表达（季度/年份/十年前）→ browse 按档期列表  ⬜ 待做
④ 以上都不命中               → 流程 B 语义找番       ⬜ 待做（G.1 路径①）
```

①② 已经在跑，③ 是一条 SQL，④ 是 `anime_profile.vec` 全库搜索（实测 43 ms）。
**四条分支的信号都是现成的、可测的、免费的** —— 没有一处需要模型来判意图。

⚠️ **③ 的时间表达识别用规则不用模型。** 「十年前的这个季度」「2016 年 7 月番」
「今年春季」这类是**有限的模式**，正则加一张相对时间表就够；
交给 LLM 反而会得到不稳定的日期，而日期错了结果整个错。

#### ⚠️ B 方案唯一的真风险：路由错了是静默的

用户问「十年前这个季度在播什么」，若被误分派到流程 C 就会返回「没认出」——
用户会以为系统很笨，而不知道是分错了路。

⇒ **响应里必须回传 `route` 字段，前端把它显示出来并允许改。**
「我理解为：查询 2016 年夏季新番 ▾」比静默猜测好得多 ——
这与 G.4 状态② 的反问是同一条「代价不对称」的逻辑：
多一次点击很便宜，自信地答错很贵。

💡 顺带：`route` 字段让**按钮和输入框可以共存** ——
按钮不是独立功能，而是**强制指定 route 的参数**（`POST /api/ask {route:"season"}`）。
A 和 B 因此不是二选一，B 涵盖 A。

#### ⚠️ 对第 5 周评测的影响（这条容易被忽略）

四条分支混在一个端点里，**「推荐的 NDCG」和「问答的准确率」会被搅在一起**。
⇒ 评测**必须直接打各条子路径**（或传 `route` 强制指定），不能只测端点。
📌 这也是 `route` 参数的第二个用途：它是评测的入口，不只是 UI 的开关。

#### ⬜ 落地顺序（都不阻塞第 5 周）

```
1. GET /api/season       一条 SQL，复用第 7 节已定的季度窗口口径   ← 最便宜
2. 流程 B 找番            anime_profile.vec + BM25 混合，G.1 路径①
3. 把四条分支合进 /ask     加 route 字段与可选的 route 参数
```

⚠️ **1 和 2 各自独立可用，先做出来再谈合并。** 反过来先设计分派器，
会在两条分支都还没有的时候去猜它们的行为 —— 与「先跑通小样本再全量」
（第 15 节原则 5）同一条。

### ⬜ `GET /api/season` —— 按档期浏览（上面第 1 步）

实测数据已经够用，缺的只是端点。「十年前的这个季度」按第 7 节的窗口口径
（前一季起点 −7 天）实测 **2016-06-24 ~ 2016-10-01 共 134 部**：

```
你的名字。57,748 · 灵能百分百 28,993 · 声之形 27,946 · 齐木楠雄 27,828
ReLIFE 21,494 · 这个美术社大有问题！19,894 · NEW GAME! 15,707
```

⚠️ **不能挂到 `/recommend` 上** —— 那个要传评分才能算偏好向量，
而「这个季度在播什么」是**无个性化的浏览**。第 7 节定义的
`season/aired/upcoming/classic` 四个模式目前只作用在 `/recommend` 的
候选池过滤上，没有独立入口。

### ⬜ 把 prompt 纳入 `llm.descriptor()` 的指纹（第 5 周之前必做）

现在 fingerprint 只覆盖 `provider / model / temperature`，
**改了 `ANSWER_SYSTEM` 或 `HYDE_SYSTEM`，指纹一个字符都不变。**
后果是第 5 周评测日志会声称两批数字同源，而实际上 prompt 已经换过了。

⚠️ 同一条纪律在别处已经做对了两次：`embed` 有指纹校验、`translate_cache`
的键里含 `PROMPT_VERSION` —— **唯独 LLM 这条漏了**。
⚠️ 时机：CLAUDE.md 写着「第 5 周评测时 LLM 也要锁死」，prompt 是被锁的一部分。
**baseline 之前改随便改，baseline 之后改一次前面所有数字作废** ⇒ 现在补最省事。

💡 顺带记一条实测结论（I.2 ②）：**「让模型看到无关问题就不回答」不需要改 prompt** ——
门控在 ① 就把它挡掉了（零模型调用），而误命中时现有的 `ANSWER_SYSTEM`
实测 4/4 都正确拒答。真正试过改 prompt 的那次（允许从资料推断）**没有效果**，
问题在上下文噪声不在 prompt。

### ⬜ 同 IP 衍生折叠 —— `/api/related` 32% 的结果是噪声

问灵能百分百会返回它自己的「REIGEN」「第一回灵能相谈所」「10周年纪念映像」。
根因是 `series_root` 只折叠 `rt=2/3`（前传/续集），没折叠 `rt=6/11/12`（番外/衍生）。
**完整数据与两个修法的取舍见 I.3 末尾。** 倾向方案 A（标题包含关系）先顶上，
方案 B（载入 `subject-relations`）留到第 6 周与那条 `rt=12` 遗留缺口一起解决。

### ⬜ 前端要加剧透提醒 —— 因为剧透门控**确定会漏**（2026-08-16 Kevin 定）

**这是一条有意接受的风险，不是待修的 bug。** 实测（吉尔伽美什(Fate) 页，
真实解析器端到端）：

```
14 个剧透框  →  spoiler_box = 0        剧透信号一个没采到
其中一条保住的 chunk：
  [460 字 · spoiler_level=0] 人物经历 > Fate/Zero | 剧透提醒 被远坂时臣…
                            ↑ 是剧透，却标成非剧透
```

🚫 **上面那句「根因是正则漏了 `剧透提醒` 模板」已被证伪（2026-08-16 复查）。**
`剧透提醒` 是**模板名**（只在 `data-mw` 属性里），它**渲染出来的正文**正是
`以下内容含有剧透成分…` —— 就是现有正则已经匹配的那句。实测全库正文里
「剧透提醒」四个字出现 **0 次**。⇒ 往正则里加它是**空操作**。完整论证见 F.4 ③。

⚠️ 而且故障方向搞反了：`SPOILER_BOX` 只做 `sub("", text)`，**只删自己匹配到的
那 27 字套话，不可能造成文本丢失**。真正在丢文本的是**表格判据**（F.4 ④，
实测 13 个剧透表 / 11,041 汉字被整表删掉）。

**Kevin 的判断：少量剧透可以接受，前端加一个提醒即可**，不为此推迟灌库。
⇒ 前端展示问答结果时，**无条件**带一句「回答可能包含剧透」，
不要依赖 `spoiler_level` 是否为 1 —— 那一列的召回率已知不完整。
📌 这个判断**不受上面的更正影响，反而更站得住**：实测 `spoiler_level>0` 的 2,529 条里
**heimu 占 2,421、`spoiler_box` 仅 150**，且 207 个剧透模板里有 113 个是不包裹内容的
独立 banner —— **剧透框这条腿比 E.6 设想的弱得多**，靠改正则救不回来。

📌 这条与第 15 节原则 2「检索前剧透门控 > 运行时 LLM 检测」不冲突：
门控仍然是主防线，前端提醒是承认它有漏网时的兜底。
⚠️ **但别把这条理解成「门控可以不做」** —— `spoiler_level>0` 的那 2,529 条
仍然挡住，漏的是没被识别的那部分。

⬜ 真要提高召回，方向是 **E.6 挂着的章节级规则**（「结局」「最终话」整节算剧透），
不是改正则。⚠️ 任何改动 chunk 文本的修法都**必须赶在重新编码之前** ——
改文本＝换缓存键＝重新花钱请求 API。

### ⬜ 🚨 语料大量是日文原文，跨语言惩罚已实测坐实（2026-08-16）

**规模**（判据：假名占比 >0.20）：

| | 日文占比 |
|---|---|
| 作品 `summary` | **39.5%**（4,247 / 10,740） |
| **角色 chunk** | **62.0%（43,381 / 69,999）** ← 比作品严重得多 |

日文简介里最热的：魔法少女小圆(59,557) · 无职转生 第2部分 · 某科学的超电磁炮S —— 全是头部作品。

**⚠️ 惩罚是真的，不是猜的。** 判据：中文查询的 top-50 里日文简介占比 vs 全库基准率。

```
全库基准                    39.2%
8 条中文查询的 top50 合计      11.5%   (46/400)
                            ────────
偏差 −27.7 个百分点 → 日文简介作品的召回率不到应有的三分之一
```
最极端的三条（「主角很强但很低调」「科幻题材的动画」「悬疑推理」）都是 **1/50 = 2%**。

⚠️ **影响范围**：`anime_profile.vec` 是 P1 三路融合的一路、流程 B 路径① 的全部依据；
而角色 chunk 的 62% 直接决定流程 C 的角色问答质量。

**为什么会这样**：Bangumi 是中文站但 wiki 由用户编辑，大量条目**直接粘贴官方日文简介
而没有翻译** —— 角色条目尤其如此（一部作品几十上百个角色，没人逐个翻）。
📌 所以「网站上看着是正常中文」和「库里是日文」不矛盾：**取决于具体条目**。

**可能的修复（都要实测后再选）：**

| 方案 | 做法 | 代价 / 风险 |
|---|---|---|
| A 查询侧双语 | HyDE 同时生成中/日两版，各检索再 RRF | 零重灌；每次查询多一次 LLM+编码 |
| B LLM 翻译后重编码 | 译 4,247 条简介 + 43,381 条角色 chunk → 重编码 | ⚠️ **会改变 `vec`**；`src/llm.py` 已就绪 |
| C 萌娘补位 | 用萌娘的中文剧情概要替换/补充 | ⚠️ **只能覆盖 26.0%**，见下 |
| D 接受 | 记录在案，第 5 周报告里说明 | 零成本 |

⚠️ **方案 C 的实测上限**：日文简介的 4,543 部里**只有 1,181 部（26.0%）有萌娘语料**。
萌娘正文确实是中文（实测《魔法少女小圆》的萌娘「剧情概要」正是其日文简介的中译），
但覆盖不够。
📌 **而角色那层萌娘一条都没有** —— 这给**阶段 06（抓萌娘角色页）添了一个全新的、
比"更详细"强得多的理由：它是中文角色语料的唯一来源。**

### 🔄 执行进度（2026-08-17）—— 做法见 **H 节**

```
✅ 剥离混排的日文尾巴    anime_profile 475 行 · plot_chunk 876 行
✅ 删除因此产生的孤儿行  37 条（+ 级联 60 条 scope）
✅ 基础设施             langclean / translate / translate_cache / translate_store
✅ 两个 loader 接线      剥离 → 换译文 → 切块
✅ 翻译 作品简介         3,786 / 3,794  (99.8%)
✅ 翻译 角色简介        40,139 / 40,261  (99.7%)
🚫 译文备份表           **不建**（2026-08-17 Kevin 定，见下）
✅ 重灌两个 loader + 重新编码   **2026-08-18 完工，见 H.7**
```

**灌库结果**：

```
                 灌库前                    灌库后
作品 summary 日文  3,833 / 10,864 (35.3%)  →     39  (0.36%)
角色 chunk   日文 42,664 / 69,962 (61.0%)  →    560  (0.81%)
库 712 → 770 MB · 28 项测试全绿 · 三批语料指纹同源 b27080d522cd9f05
```

**最终产出（2026-08-17）**：缓存 **43,932 条 / 50 MB** · 冗余 0 · 质量闸复检 0 条不合格 ·
**整体残留假名 0.0098**（MT 单跑时是 0.0147 —— 多模型不只是快，质量也更好）。

| 模型 | 产出 | 占比 | 残留假名 |
|---|---|---|---|
| tencent/Hunyuan-MT-7B | 17,153 | 39.0% | **0.0072** |
| Qwen/Qwen3-8B | 12,734 | 29.0% | 0.0144 |
| deepseek-ai/DeepSeek-R1-0528-Qwen3-8B | 7,266 | 16.5% | 0.0090 |
| THUDM/GLM-Z1-9B-0414 | 6,779 | 15.4% | 0.0164 |

⚠️ **MT 只占 39%（角色层仅 12.8%）** —— 它并发上限只有 4，而三个协作模型合计 32 路。
译文风格的混合程度比原计划高，**落库前抽查要按这个比例取样，不能只抽 MT 的**。

🚫 **`sql/008` 译文备份表不建（2026-08-17 Kevin 定）** —— 改为手动备份整个
`data/interim/`。⚠️ **那么 `data/interim/translate_cache/` 就是译文的唯一副本**，
丢了是**重翻 8 小时**（编码丢了只是 ¥0.19 / 12 分钟，量级完全不同）。
📌 建表脚本 `sql/008_translation.sql` 与 `translate_store.sync_to_db/restore_from_db` **保留不删** ——
将来改主意时是一条命令的事，删掉反而要重写。

⬜ **剩余 131 条未翻**（作品 8 · 角色 123 = 0.30%），清单在
`data/interim/untranslated.csv`（id/名称/所属作品/字数/假名率/原文）。**建议不处理**：
- 作品那 8 部里 **4 部原文就是中文**，是 `is_japanese()` 被标题里的假名误判选进来的，
  质量闸随后正确地拒绝了「中译中」—— **两道判据先后各错一次正好抵消，库里留的是对的**
- 角色 123 个里 **34 个是短文本**（<40 字），译文里一个平假名人名就把比例顶过 0.20 阈值
  （`ひふみが飼っているハリネズミ。`）—— 这是 `looks_untranslated()` 的**已知误伤**，
  放宽阈值就会漏进真回声，为 0.08% 的收益动一条验证过的判据不划算
- 还有一类**根本没法翻**：`「うーら　めーた　ぱーら…」`（咒语拟声）

⚠️ **译完必须重跑两个 loader 才会把译文灌进库**，并**重新编码**（文本变了向量必须跟着变），
参照 F.4 ① 分批 + 普通 `VACUUM`。

### 📌 Kevin 已定：**日语语料要从库里清掉**（2026-08-16）

⚠️ **执行前必须先明确「清掉」的含义 —— 两种理解后果完全相反：**

| 理解 | 动作 | 后果 |
|---|---|---|
| **纯删除** | `DELETE` 日文行 / 把 `vec` 置 NULL | ⚠️ 4,247 部作品**彻底退出**向量召回（不是变弱，是消失）；角色语料从 69,999 砍到 26,618（**−62%**） |
| **译后替换**（推荐） | LLM 译成中文 → 重编码 → 覆盖原行 | 库里同样不再有日文，但**覆盖率不掉**；`src/llm.py` 已就绪 |

📌 **两者都满足「库里没有日语」这个要求，但纯删除会连数据一起丢。**
实测那些作品现在虽被打折（召回率 11.5% vs 基准 39.2%），**仍然能被召回**；
删掉就是 0。⇒ 除非你明确要纯删除，**默认按「译后替换」执行**。

⚠️ **时机：应该在第 5 周评测开始之前做完，不是之后。**
这一条与本文档其他「等 baseline」的纪律**方向相反**，理由也不同：
那些是「不确定要不要改，所以先别动，免得污染对照」；
而这条**已经决定要改**，那就该让 baseline 直接建立在最终语料上 ——
先评测再改，等于让第 5 周的全部数字失效。

⬜ **动手前要做的两件事**：
1. 估算翻译成本（4,247 条简介 + 43,381 条角色 chunk），走 `src/llm.py`
2. ⚠️ 重编码会改变 `build_meta` 的行数与指纹，`plot_chunk` 的 md5 跳过逻辑
   会把**所有被翻译的行**判为已变化 → 触发全量重写。参照 F.4 ① 分批灌 + 普通 VACUUM。

---

⚠️ **在上面这件事执行之前，不要做别的会改 `vec` 的改动** —— 会和它撞车。
📌 **第 5 周报告里这条是好材料**：「我们量出跨语言惩罚 −27.7 个百分点，
据此决定统一语料语言」比孤立的 NDCG 数字有说服力得多。

⚠️ **判据必须用 kana>0.20，不能用 >0.03。** 实测 >0.03 会把中文简介误判成日文 ——
《Fate/Zero》(0.033) 是中文里引用了「圣杯戦争」、《寒蝉鸣泣之时 礼》(0.036) 引用了
《羞晒し编》。两个阈值差 2.8 个百分点，全是假阳性。

📌 **一个我犯过的归因错误，记下来防止重犯**：本条最初的发现路径是「六个 LLM 都没召回
《孤独摇滚！》」，我当场归因为「它的简介是日文」。**错的** —— TV 本体（id=328609）
的简介是中文，日文的是两部剧场总集篇；我按热度排序查询后被 `tail` 截断，只看到剧场版
那行就下了结论。⇒ **多条同名条目的抽查必须看全，不能只看输出末尾。**
（跨语言惩罚本身是真的，但那是后来独立测出来的，与孤独摇滚无关。）

### ⬜ 修 `is_name_echo()` 的中文阈值（2026-08-18，低优先级）

判据 `len(ns) <= 12` 在日文上标定，中文语料下误伤 167 条有信息的短简介
（9 个角色因此整个消失）。**完整论证与数据见 H.7 末尾。**
⚠️ 与季度更新绑在一起做 —— 重跑 `build_char_chunks` 那一步两者共用。

### ⬜ 季度更新功能（2026-08-17 Kevin 提出，动手前再详细讨论）

**目标两件事**：季末拉一次数据，① 收录**下个季度**的新番资讯 ② 更新**本季度**已有作品的信息。
两个来源都要：**Bangumi dump + 萌娘百科**。

📌 **架构上大部分已经就位** —— 下面这四条是季度更新最难自己补的，而它们都是既有产物：

| 能力 | 在哪 | 为什么关键 |
|---|---|---|
| 所有 loader 幂等 | 三个脚本各管各的列，实测重跑不改行数 | 增量更新的前提 |
| **md5 跳过未变行** | `build_plot_chunks` / `build_char_chunks` | 只重写真变了的行，避免 F.4 ① 那种全表 UPDATE 把 TOAST 翻倍 |
| **萌娘 `lastrevid`** | `fetch_moegirl.py` 已经在存 | **一次请求 50 个标题就知道哪些页改过**，不用重抓 |
| 三层缓存 | `embed_cache` / `translate_cache` / catalog npz | 只有新增内容才花钱 |

⇒ **「更新本季已有作品」基本是免费的**，重跑现有脚本即可。真正要解决的是下面这些。

#### ⚠️ ① `done>=50` 把新番挡在门外 —— 这是唯一的架构冲突

```python
# src/candidates.py:90
return (rec.get("favorite") or {}).get("done", 0) >= MIN_DONE   # MIN_DONE = 50
```

下季新番收藏数是 0，`is_candidate()` 直接返回 False。**季度重跑 dump 收不进新番** ——
这正是「`mode=upcoming` 实测只召回 2 部」的根因，**不是数据滞后，是筛选口径挡的**。

⚠️ **不能简单调低阈值**：`done>=50` 是好阈值（第 13 节：它一次清零了无 tag、无评分、
无人看过三个数据质量问题），调到 0 会放进来几万条同人动画和 MV。
⇒ **加一条并列的准入路径**，让新番凭「即将播出」入场而不是凭热度：
`done >= 50  OR  air_date 在未来 N 个月内`。

⚠️ **这会改变候选集口径，而候选集是「唯一事实来源」。** 第 5 周四条 baseline 必须跑在
同一个候选池上 ⇒ 要么在评测前定死，要么**给评测单独冻结一份快照**。
**这是唯一需要人来拍板的点，其余都是纯工程。**

#### ⚠️ ② tag / staff 词表漂移 —— 最隐蔽的一条

`tag_vec` 是 **308 维**、`staff_vec` 是 **sparsevec(1933)**，维度写死在 `sql/002` /
`sql/006` 的列定义里。新一季必然带来新 tag 和新制作公司：

```
词表不变 → 新 tag 被静默忽略（新番的特征被抹掉，不报错）
词表增长 → 维度变了 → ALTER 列宽 + 全库 11,453 行重算
```

⇒ 得明确定策略：**季度更新冻结词表，每年重建一次**。不定的话它会以
「新番推荐质量莫名其妙偏差」的形式出现，而且很难归因。

#### ⚠️ ③ 没有删除逻辑，孤儿行会累积

loader 只 upsert 不 delete（唯一例外是 `load_profiles.py` 按 subject 范围重写 alias）。
H.2 剥离日文那次已经实测出过 **37 条孤儿 chunk** —— 切块结果一变，旧的
`(character_id, chunk_no)` 就没人认领。季度更新会反复制造这类行。
⇒ 需要一个收尾步骤：**本次管道产出之外的行一律删除**。

#### 重算链条（漏一步不报错，只是静默用旧数据）

```
dump → candidates → load_profiles → backfill_staff/anilist → series_map
     → tag_vec → staff_vec → 翻译 → embeddings → mmr_rank/cluster
```

💡 `build_catalog()` 的 npz 缓存键已含 `max(updated_at)`（B 节），能正确失效。
⚠️ 新增的日文简介走 H.6 的四模型管道，已就绪。

#### 小结

| 目标 | 现状 | 要补 |
|---|---|---|
| 更新本季已有作品 | ✅ 直接可用 | 孤儿清理 |
| 更新萌娘页 | ✅ `lastrevid` 已在存 | 比对逻辑（几十行） |
| **收录下季新番** | ❌ **被 `done>=50` 挡住** | **准入规则加一条并列路径** |
| 新番的萌娘语料 | ⚠️ 部分 | 新条目要走标题解析 + rescue 三条规则（F.5 那个换机器陷阱同源） |
| 新增日文简介 | ✅ 四模型管道已就绪 | 无 |

**结论：能做，工作量在「准入规则」和「词表策略」两个决策上，不在代码量上。**
实现大致是一个 orchestrator 按顺序跑现有的那串，加孤儿清理和 `lastrevid` 比对。

### ⬜ 英文名回填 —— 英文版前端的前置（2026-08-16 Kevin 提出计划）

Kevin 计划做**英文版前端**，所以作品名必须对得上。实测 `name_en` 只有 **45.5%**，
但**零抓取就能到 71.3%**（论证见第三部分「与本文档原计划的两处偏离 ①」的标注）。

**回退链，按优先级：**

| 层 | 来源 | 可补 | 需要抓取 |
|---|---|---|---|
| 1 | `name_en`（AniList english） | 5,206 | 已有 |
| 2 | `name`（若为拉丁字母 → 欧美/国创原名） | +1,410 | ❌ 零抓取 |
| 3 | `infobox_alias` 里的英文写法 | +1,552 | ❌ 零抓取 |
| 4 | ⬜ AniList `title.romaji` | 最多 +1,239 | ✅ 约 125 次请求 |
| 5 | 兜底：显示原名 + 罗马音转写 | — | — |

⚠️ **第 4 层要改 `backfill_anilist.py` 的 GraphQL**：当初只取了 `title.english`，
把 `title.romaji` 一并取回来即可。那 1,239 部是「有 id 但 AniList 没填英文标题」的。

⚠️ **第 3 层有个质量问题，先不纠结**：一部作品可能同时有 `Attack on Titan`（英文）
和 `Shingeki no Kyojin`（罗马音），自动选很难分。罗马音在动画圈也是通用写法，不算错。

📌 **剩下的 3,285 部有明显规律**：主系列有英文名，**OVA / 新编集版 / 剧场版这类
衍生条目没有**（`Re：从零开始的异世界生活 新编集版`、`我的青春恋爱物语果然有问题 OVA`）。
按热度分布偏长尾但不极端（第 1 档 31 部 / 第 5 档 426 部）。

⚠️ **不要现在做。** 这是纯数据层回填，任何时候做代价一样，且不阻塞阶段 05；
而前端本来就冻结在 v0、要最后一次性写。
💡 但**判据要记住**：`name_en` 目前只用于 `/api/anime/{id}` 的展示字段，
**不进 `search_tsv`、不进 `alias`、不参与任何打分** —— 英文搜索能力靠的是
`infobox_alias`（17,416 行），与这个回填无关。所以它纯粹是显示层的事。

### ⬜ `summary` 噪声 —— 已量出来，但**第 5 周之前不要动**（2026-08-16 记）

自检实测：**1.0% 短于 20 字**（「18X版」「TV二期。」这类）· **0.5% 含 URL** ·
**128 部共用重复 summary**。

⚠️ **现在清洗会改变 `vec` 的非空集合，直接污染第 5 周 baseline 的口径** ——
与「热度权重必须等 baseline 跑完」「tag 共现只统计不调权重」同一条纪律。
⇒ 留到第 5 周，作为一个 ablation 变量处理，不要顺手改。

### ⬜ 阶段 04b：角色页链接提取（阶段 06 的前置）

从已抓的 2,301 页 HTML 里提角色页链接（抽样 10.2 个/页），并用 `prop=info`
求和 `length` **算准阶段 06 的体积**。⚠️ 不做这一步，阶段 06 的「15–40 小时」
就只是个估数 —— 而那是本项目最贵的一笔墙钟时间开销。详见第一部分六阶段计划。

### ⬜ 把 dump 的 `episode.jsonlines` 灌进来（2026-08-15 新增）

实测有 **108,835 条分集 description**（中位 193 字，涉及 10,630 个 subject），
字段 `name` / `name_cn` / `description` / `airdate` / `duration` / `subject_id` / `sort`。
用来答「第 X 话讲了什么」—— **比萌娘百科的各话表（只有标题）丰富得多，且是权威来源**，
所以 E.4 判据 ② 说的「各话列表不从萌娘百科拿」在这里有了正面的替代方案。
⚠️ 全库只有 6% 的 episode 有 description，**先按我们的 11,453 部算清楚覆盖率**再动手。
⚠️ 这是独立的一条数据线，别混进 `plot_chunk` 的批次 2。

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


---

# 第五部分 · 设计文档（第 1–15 节）→ [docs/design-doc.md](docs/design-doc.md)

> **原始设计稿，不是当前状态**，整体移出。保留是因为论证过程仍然有用
> （为什么否决 AWS / 为什么 `done>=50` 是好阈值 / 口径怎么一步步收紧）。
> 已知被推翻处均在原文标注（Render→Vercel · 聚类→MMR · 正文不进 Neon→进 Neon ·
> HyDE 默认开→默认关 · 免费层 500 MB 悬崖→付费线性计费 等）。
> **「第 N 节」引用都指向那个文件；编号是稳定锚点，勿重编。实操以本文第二部分为准。**

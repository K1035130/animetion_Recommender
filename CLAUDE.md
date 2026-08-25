# 动画推荐系统 — 项目前情提要

> 本文档为项目决策记录与执行计划，供 Claude Code 作为初始上下文引入。
> 所有技术选型均已论证完毕，**除非有新证据，不要重新讨论已决事项**。

---

## 🧭 怎么读这份文档（2026-08-25 二次瘦身后的结构）

CLAUDE.md 只保留**每次开工需要的**：现状、操作手册（A–D）、待办。
归档性内容（论证过程、实测记录、原始设计稿）已**逐字**拆到 docs/，冲突时按时效判断：

🚨 **2026-08-25 又移走了 1,812 行（3,543 → 1,721）。** 第四部分「待办」里
有 69% 是**已经做完**的小节，加上三个「这天做完的」日志 —— 全部逐字搬进
[docs/history.md](docs/history.md)。**内容一个字没删，只是换了地方**：
找「某个功能当初是怎么做的、踩过什么坑」去 history.md，本文只答「现在什么状态、
接下来做什么」。⚠️ 以后完成一件事，**把详情移进 history.md 并在第一部分留一句摘要**，
不要留在第四部分 —— 它上次就是这么涨到 1,507 行的。

| 在哪 | 内容 | 时效 |
|---|---|---|
| 本文 · 第一部分 | 现状与下一步 | 最新，每次干完活就更新 |
| 本文 · 第二部分 A–D | 模型 / 部署 / 脚本 / 环境（操作手册） | 最新，**实操以这里为准** |
| 本文 · 第三部分 | 已完工论证的**结论速查**（勿回退） | 已定案 |
| 本文 · 第四部分 | 待办 | 活的清单，**只放 ⬜** |
| [docs/corpus.md](docs/corpus.md) | E / F / H 节：萌娘语料 · plot_chunk · 语料转中文 | 已完工的实测记录 |
| [docs/retrieval.md](docs/retrieval.md) | G / I 节：检索层设计与实现 | 已完工，第 5 周评测的主要依据 |
| [docs/history.md](docs/history.md) | 第三部分详情 + 第 3/4 周清单 + **已完工的功能与改动**（账号系统 / 前端 / 声优 / 找番 / 意图校验 / 语料清洗 …） | 已定案，论证过程 |
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

## 📍 当前进度（更新于 2026-08-24）

**语料层已收工**：阶段 06 角色页两批全部灌完，解析层又清过两轮噪声
（模板 chrome + 套话前言 · 元信息章节豁免 heimu 门控）。
声优问答、意图路由、英文档期查询都已上线。
✅ **单一入口四条分支全部齐了（2026-08-24）**：voice / season / ask / **find**，
详见「流程 B 找番」那节（第四部分）。**「① resolve() → 流程 C · ② related
→ 关联查询 · ③ 时间表达 → season · ④ 都不命中 → 找番」四条分支的落地到此收工**。
✅ **同一天又加了一层「意图校验」**（`llm.classify_intent()`，Qwen/Qwen3-8B）——
起因是打分表之外自己测出两个新洞：① 自动分派下 `resolve()` 会被短别名字符串
巧合命中，把纯闲聊答成某个角色的问答；② `season`/`find` 按钮选错时会"自信地
答非所问"（`voice` 靠自身空结果检测天然幸免，另两个没有）。现在**每条请求都会
先过一次这道校验**（"auto + 所有按钮都校验"，Kevin 定），215 项测试全绿，
详见下面「意图校验」那节。**代价是 voice/season 从零模型瞬间返回变成要等
一次 LLM**（实测 0.3–1.1s，个别到 4.9s）——Kevin 已经知情接受。
剩下的都是前端接线 —— ✅ **已于 2026-08-24/25 全部写完**（见开头「前端现状」）。
✅ **60 题打分表已填完并出分（2026-08-23 晚）** —— 结果写在
**[week5-eval-report.md §7](docs/week5-eval-report.md)**。**第 5 周到此全部收口。**
🎯 **检索命中 30.8% → 50.0%**（检索侧两轮可比），**12 道 `n→y` 全部落在四项已知
改动的作用范围内**（剧情简介席位 · 元信息豁免 · SONGS_SEAT · 阶段 06 角色页）。
⚠️ 但 **[4] [11] [12] 被前两项同时碰到，不能单独归因** —— 能单独归因的是 9 道。
**两项窄触发一旦重叠，可核账性就止步于"范围"而非"归因"**（§7.2）。
🚨 **但脚本报的「幻觉 84.6%」不能按字面用 —— 那是量具问题，见下面「评测判据已漂」那条。**
阶段 05 检索层 ①②③④ 的实现细节与 bug 记在 **I 节**；G 节保留为设计依据。

📌 **送进 LLM 的资料现在有三个来源**，不只是 `plot_chunk`：
```
plot_chunk        向量召回 + alias 直取，走 rerank 排序      chunks
作品简介          anime_profile.summary，剧情梗概类问句触发    aux ← 2026-08-23 加
related 关联查询   staff/studios 结构化字段，零模型            aux
```
⚠️ **后两者不在 `chunks` 里，而是挂在 `Answer.aux`** —— 凡是新增"送进 LLM 的
信息源"都必须挂上去，否则打分表看不见它（docs/history.md「评测口径 bug」那一节）。

```
POST /api/ask       流程 C 剧情问答，四步管道         src/retrieve.py
GET  /api/related   同作者/导演/公司的其他作品，零模型   src/related.py
GET  /api/voice     某声优配过哪些角色，零模型          src/voice.py
POST /api/ask       ⬆️ 现在是**单一入口**：自动分派 voice / season / ask
                    三条分支，或由 route 参数强制指定       src/router.py
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

### ▶️ 下次开工从这里接（2026-08-25 收工）

## 👤 最新一轮：个人中心页（2026-08-24，Kevin 提出）

登录后的个人页：账号信息 + 改用户名/改密码 + 查看和修改已打分的动画。
`web/src/AccountPage.tsx` + 三个端点（`GET /api/ratings/detail` ·
`PUT /api/auth/username` · `PUT /api/auth/password`）+ `ratings.list_detailed()`。
测试 313 → **329**。**完整记录见docs/history.md「个人中心页」那一节**，三条先知道的：

```
🚨 改用户名也要当前密码      XSS 偷不走 cookie 但能拿它发请求
🚨 改密码踢不掉别处的登录态   JWT 无状态，已在前端明写出来；要修得加 token_version
🚨 用 PUT 不是 PATCH        CORS 的 allow_methods 里没有 PATCH，本地跨源会被预检拦
```

⬜ **季度更新仍未开工**（本轮之前 Kevin 提过要做，随后改为先自检 + 个人页）。
那三条待解决的点见本文「季度更新功能」一节，自检时已逐条验证仍然成立：
`air_date > 今天` 只有 2 部 · 两个 loader **零 DELETE 逻辑** · `lastrevid` 已在库里。
⚠️ 另有一条自检新发现：**有条目只有 `air_year` 没有 `air_date`**
（`air_year>=2027` 有 6 部，而 `air_date` 在未来的只有 2 部）——
准入规则那条「air_date 在未来 N 个月内」的并列路径要考虑 air_date 为 NULL，
否则这批会漏。

## 🔊 上一轮：声优回答升级（2026-08-25，Kevin 提出）

`voice` 分支从「粗暴返回名单」改成「加权排序 + LLM 组织语言」，
并支持「花泽香菜**最近**配过什么角色」这类带时间限定的问法。
**完整记录见docs/history.md「声优回答升级」那一节**，这里只留三条最该先知道的：

```
🚨 「纯按热度排序」实测更差，已否决   钉宫理惠「神乐」从第 3 掉到第 21
                                   ⇒ 改用 fav_done × 役别权重
🚨 voice 生成改用 Qwen3-8B          14B 26–37 s → **8B 5.7–6.5 s**，且客观判据更干净
🚨 前端 hasStructured 曾把 answer 藏掉  改后端行为要回头查前端的旧优化
```

✅ **模型这条已定**：Kevin 先说「延迟可以接受」，随后又说「换成 8B 的看看
是不是会快一点」⇒ 跑了两轮 A/B，**8B 胜出且不是只赢在延迟**（详见第四部分
那节的「为什么最后换成了 8B」）。voice 现在 `VOICE_PROVIDER = Qwen/Qwen3-8B`，
独立于 PRIMARY/FALLBACKS 链、无 fallback（挂了回落展示配役表）。

## 🎉 上一轮：前端全部写完 + 账号系统上线 —— 第 6 周的两大块都落地了

**这一轮（2026-08-24 深夜 ~ 08-25）做完的**：

```
✅ 前端不再冻结在 v0        首页 / 推荐三子页 / 问答页全部重写，见下「前端现状」
✅ 账号系统                注册 · 登录 · 评分持久化 · 问答配额，见docs/history.md「账号系统」
✅ 用户名取代邮箱          根因是「没有发信能力」，见 docs/history.md
✅ 测试 247 → 297 项全绿
```

### 📌 前端现状（**「冻结在 v0」那条已作废**）

第三部分那句「前端刻意冻结在 v0，不要顺手美化」是 2026-08-13 定的，
**目的已经达成**（逻辑全部做完后一次性写），2026-08-24 起正式解冻。
现在的结构：

```
App.tsx            首页（标题 + 两个功能方块）+ 顶栏 + 账号菜单
RecommendHub.tsx   动漫推荐：三个大方块（填写问卷 / 动画打分 / 开始推荐）
  QuestionnaireCards.tsx   问卷，AWS Skill Builder 单元测样式的方形选择框
                           ⚠️ 第一题是**资历题**，选完才拉题 —— 不先问就只能
                              用默认 experience，出题年份范围是错的
  RateSearch.tsx           搜片名直接打分（补问卷抽不到的冷门番）
  RecommendResults.tsx     模式/排序 + 推荐列表
AskPanel.tsx       动漫问答，Claude 网页版布局（消息区滚动 + 输入框钉底）
AuthDialog.tsx     登录/注册弹层
AccountPage.tsx    个人中心：账号信息 + 改用户名/改密码 + 已打分动画的查改
                   ⚠️ 入口是顶栏那个用户名（点它进来）——它因此不能再
                      `hidden sm:inline`，否则小屏上没有入口
session.tsx        🚨 会话与评分的**单一入口**，见下
session-context.ts context 与 hook（与 provider 分文件是为了 Fast Refresh）
usageNotice.ts     问号按钮唤起的使用须知纯文本
```

🚨 **`session.tsx` 是前端最该先读的文件。** 上层组件**不知道用户登没登录** ——
它们拿到的永远是 `{answers, setAnswer}`，数据存 localStorage 还是同步到账号
由它决定。这是服务端那条「评分随请求传入，推荐链路不知道评分来自哪」
铁律在前端的对应物。⚠️ 一旦让某个组件自己写 `if (user) ... else ...`，
这条铁律就破了，而且会破在多处、各自漂移。

⚠️ **评分同步是防抖批量（方案 B，Kevin 定）**：1.2 秒防抖 + `visibilitychange`
时 flush。**防抖必须配 flush** —— 用户点完最后一题立刻关标签页是最常见的
使用方式，只防抖不 flush 那一批就永远丢了。

### 🚨 部署前必读：Vercel 上一共要配 **3 个**环境变量

```
DATABASE_URL          本地 .env 里带 -pooler 的那条，原样复制
SILICONFLOW_API_KEY   本地 .env 里那个（embedding / LLM / rerank **共用一个 key**）
AUTH_SECRET           新生成，见 .env.example
```

⚠️ **`DATABASE_URL_DIRECT` 不要配到 Vercel。** 线上代码零引用（`db.connect()`
只有 `scripts/` 用，而 `.vercelignore` 已排掉 scripts/）。更要紧的是
`pool_dsn()` 写的是 `DATABASE_URL or DATABASE_URL_DIRECT` —— 两个都配的话，
`DATABASE_URL` 哪天被误删/拼错会**静默退回直连**，服务照常起来、功能看着
正常，只是在悄悄耗尽 Neon 的连接数。只配一个则直接抛错，立刻可见。
⚠️ **`CORS_ORIGINS` 也不要配**（线上同源，那段中间件不参与）。
⚠️ 改环境变量后**必须 redeploy 才生效**。

📌 **三个变量各有一条独立验证路径，别只测 /health 就以为全好了**：
`/api/health` 回 `catalog_size=11453` → DB 通；注册一个账号 → AUTH_SECRET 通；
问一条剧情问题 → SILICONFLOW_API_KEY 通。

### ⬜ 下一件事（三选一，都不阻塞）

```
1. 同 IP 衍生折叠     rt=6/11/12 未折叠，/api/related 32% 是自家衍生品
                     ⚠️ 方案 A（标题前缀）已被证伪，走方案 B（读 dump 的
                        subject-relations），见 docs/history.md
2. classify_intent 打分表   这轮加的意图校验还没有人工评分（find_gate 那轮的
                           9 题覆盖不到「按钮选错」「离题闲聊」这些新场景）
3. 季度更新           准入规则 done>=50 挡住新番，见 docs/history.md
```

---

## ✅ 流程 B 找番 + 意图校验层都已上线，当天两轮迭代都验证过

**第一轮**：`src/find.py`（语义找番）+ `src/llm.py::find_gate()`（LLM 判断值不值得
展示检索结果）+ `server/main.py` 接线（按钮强制 / 自动兜底 / 回落保护）。
**已打分**：`docs/eval-find-gate-sample.md` 10 题，Kevin 填完——**9/9 适用题
gate 判断与直觉吻合，9/10 结果满意**，唯一的 `n` 是 [7]（离题闲聊被 `resolve()`
误匹配到某个角色，`find_gate` 根本没被调用，不在它的职责范围）。

**第二轮（当天同一天，由 [7] 引出）**：`llm.classify_intent()`（Qwen/Qwen3-8B）
——每条请求都先判一次意图，堵住了 [7] 那类 `resolve()` 误匹配，也堵住了
`season`/`find` 按钮选错时"自信地答非所问"的洞。**215 项测试全绿**，详情、
延迟实测、设计取舍（auto 全信/按钮只拦不改判、voice 分支为什么不看 intent）
都记在下面「意图校验」那一节——**这节比找番本身改动更大，回头改东西优先看这里**。

✅ **待办 1 已补（2026-08-24）**：`tests/test_llm.py`（16 项，find_gate/
classify_intent 的解析容错 + provider/prompt 接线，monkeypatch 掉网络层）
+ `tests/test_find.py`（4 项，find() 的管道正确性，拿一部作品自己的向量当
"假查询"验证语义有意义，不是纯管道测试）+ `tests/test_ask_intent.py`
（12 项，`/api/ask` 的路由 wiring——覆盖 auto 模式的 intent 覆盖、三个按钮
各自的"选错拦下来"、**voice 分支空结果回落必须完全不看 intent** 这条真实
回归、以及校验/门控本身挂了时的优雅退化）。**215 → 247 项全绿**
（`uv run --group etl python -m pytest tests/ -q`，116.70s）。
⚠️ **这些测的是"路由选对了分支"，不是"LLM 判断准不准"** ——
后者仍然只有 `find_gate` 那轮的打分表，`classify_intent` 这轮还没有。

⚠️ **待办（明天继续，今天先不做）**：
1. `docs/eval-find-gate-sample.md` 没有覆盖今天第二轮加的场景（[7] 离题闲聊、
   season/find 按钮选错、[10] 那类"看着离题实则有意图"的边界情况）——
   要不要为这轮单独出一版新打分表，还是先补测试，看 Kevin 的意见。
2. voice/season 的延迟从零模型瞬间变成 0.3–4.9s（意图校验那节有完整数据），
   如果觉得代价太大，需要重新评估——**目前是 Kevin 已知情接受，不是待修的 bug**，
   但没有做长期观测（比如高峰期 Qwen3-8B 延迟会不会更不稳定）。

📌 **60 题打分（§7）与本轮改动互不相关**：第 5 周工作流 B 五项全部完成，
指纹口径不变——本轮 `prompts=871d63553987ad3e`，基线 `07cc60bcd5216704`（08-19），
**检索侧可并排，生成侧不可**，重新出分是零成本的只读操作：

```bash
uv run --group etl python scripts/eval_answerability.py score --sheet docs/eval-answerability-rescore-v5.md
uv run --group etl python scripts/eval_answerability.py score --sheet docs/eval-answerability-sheet.md   # 基线对照
```

**下一件** → 上面「待办」三条二选一开工；再往后是「同 IP 衍生折叠」
（rt=12 折叠）或前端统一接线。

---

### 🚨 评测判据已漂：「幻觉」这个数现在是错的（2026-08-23，未修）

`eval_answerability.py` 把**「未命中却给了答案」判为幻觉**、把**「拒答」判为
`answer: r`**。这两条定义于旧 prompt 时代，而 08-23 那次 `ANSWER_SYSTEM` 改动
（「已知剧情推进到」）的**设计目的**恰恰是把拒答转成有据的部分回答 ⇒

```
幻觉        22.2% → 84.6%     26 道逐条读完，**0 道是编造**
路由类拒答   25%   → 0%       那 8 道其实都在拒答，只是拒答里带了解释
```

🚨 **这与「打分表看不见 `aux`」是同一族的问题：不是系统变差，是量具没跟上系统。**
两次都只在**人去读原文**时才暴露 —— 指标本身看起来完全正常，只是含义已经漂了。
⇒ ⬜ 建议改判据（**会改变指标定义，等 Kevin 拍板**）：
「幻觉」应判**回答里有没有资料支持不了的实质断言**；
「正确拒答」应把带解释的拒答算进去。完整论证见 **§7.3**。

---

## ✅ 阶段 06 已收工（2026-08-23 灌库完成）

第二批抓取与灌库都已做完，**语料层这条线到此结束**。留存的操作手册见下面
「抓完之后照这三步走」——季度更新时还要照跑一遍（增量，md5 会跳过未变行）。

```
抓取   7,407 个文件 · manifest 7,371 唯一 pageid（63 行重复是断点续跑追加，正常）
解析   7,363 页有正文 → 45,599 chunk（8 页零产出）
灌库   新增 15,053 · 变更 0 · 未变 30,534 · 孤儿 0 · 冲突 0/0 · ¥0.26
```

⚠️ **预演又一次拦下了哆啦A梦**：唯一被丢弃的 1 页正是 pageid 153278
「哆啦A梦」，原因是**撞作品页**（`series_pageids` 守卫）。第一批靠它抓到过
同一个 bug —— **这一步不能省**，它是唯一会在灌库前发现「角色与作品同名」的地方。

⚠️ **别用 `cmd | tail` 判成败**（A.7 记过），也**别把 shell 的 `timeout` 当成
   agent 工具的超时** —— 实测工具自己 120 秒就 SIGTERM 了，
   `timeout 590` 那层根本没机会生效。
   💡 但**没有造成任何损失**：脚本是三段式（读库 → 长耗时 API → 写库），
   被杀时还在编码阶段，写库连接尚未建立；已编码的 6,016 条落进了
   `embed_cache`，重跑直接命中。**这是缓存层第二次兑现价值。**

### 抓完之后照这三步走（都跑过一遍，第一批就是这么灌的）

```bash
# 1. 解析（纯本地，约 40 秒）
uv run --group etl python scripts/parse_moegirl.py --kind character

# 2. 编码（走缓存，只请求新增的；第一批 30,391 条花了 9 分钟 / ¥0.55）
#    用 build_plot_chunks 的 load_corpus 取文本，别自己拼 —— 作用域和
#    「角色与作品同名」的排除都在里面
uv run --group etl python scripts/build_plot_chunks.py --kind character --limit 1  # 先探
uv run --group etl python scripts/build_plot_chunks.py --kind character            # 真灌
```

🚨 **第 2 步是写库，执行前必须把影响行数报给 Kevin 确认**（他的规矩，
memory 里也记着）。第一批的报法：新增 N 行 · 变更 N 行 · 孤儿 N 行 ·
冲突必须为 0 · 灌完 plot_chunk 会涨到多少 · 库多大。

⚠️ **灌库前一定要跑一次预演**，第一批就是靠它抓到「哆啦A梦」那个 bug 的：
```python
known = B.preflight(conn); have = B.existing_digests(conn); ex = B.series_pageids(conn)
pages, chunks, scope = B.load_corpus(known, None, "character", ex)
# 断言：与 have 的 (pageid, chunk_no) 冲突必须为 0
```

### 灌完之后

```
1. 跑一轮评分测试（Kevin 定：语料改动攒在一起，评测只跑这一次）
   ✅ **Kevin 已定（2026-08-23）：不重跑生成侧，在报告里注明指纹和日期。**
      60 题基线的 answer 列跨了两版 prompt（55 题是 08-19 旧 prompt）⇒
      报告里必须写清每一段用的是哪个 `llm.descriptor()` 指纹与日期，
      并**标明哪些数字不可直接并排比较**。
      📌 指纹现已覆盖两条 system prompt（见 docs/history.md），所以"是 prompt 变了
      还是模型变了"能分清 —— 这正是当初补指纹的用途。
2. 流程 B 找番（G.1 路径①）—— 单一入口只差它这一条分支，
   做完前端才好放第四个按钮（别放没实现的按钮，用户点一次就再也不点了）
```

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

#### 流程 B · 找番（语义检索）—— ✅ 2026-08-24 已实现（与本图当初的设想不同）

⚠️ **本图是第 4 周写的原始设想，两处都被后续实测推翻，按当前实现改写如下**：
HyDE 默认关（week5 §2：NDCG 0.638→0.471，p=0.012）、BM25 混合融合已否决
（`scripts/eval_find.py`：转述查询 NDCG semantic 0.576 → rrf 0.448，净负收益）。
实现细节见docs/history.md「流程 B 找番」那节。

```
用户输入「有没有主角很强但很低调的番」（resolve() 认不出任何具体作品/角色）
 │
 ├─[LLM 调用]──► find_gate()：这句话值不值得展示找番结果？（是/否二选一）
 │               判"否" → 返回套话「描述有些模糊不清，能再具体一点吗？」，到此结束
 │
 ├─[Embedding 调用]──► 编码原始查询（不做 HyDE 改写）
 │
 └─[查库]──► pgvector 搜 anime_profile.vec（语义腿，纯语义，不混 BM25）
             → 续作折叠 + nsfw 过滤（复用 recommend_sql.score()）→ 结果
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
   │  流程 B 找番  ──► LLM + Embedding API    ✅ 已上线 2026-08-24 │
   │  流程 C 问答  ──► LLM + Embedding API    ✅ 已上线 10–45 s   │
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
| [scripts/parse_moegirl.py](scripts/parse_moegirl.py) | 解析上一步的 HTML → `data/interim/moegirl_chunks.jsonl`。**不写数据库** —— 切分粒度要由它的实际产出来定，见 E.4。⚠️ `--kind character` 切到角色页（`moegirl_char/` → `moegirl_char_chunks.jsonl`）；**切分/清洗规则两者共用**，唯一实质差别是作用域来源：作品页靠标题解析，角色页用抓取时记的 `series_roots` |
| [scripts/build_plot_chunks.py](scripts/build_plot_chunks.py) | `moegirl_page` / `plot_chunk` / `plot_chunk_scope` 三张表 + `build_meta['plot_chunk']`。幂等，靠 md5 比对跳过未变化的行（F.4 ①）。⚠️ `--kind character` 灌角色页：**零 schema 改动**（sql/007 第 1 周就把 `moegirl_page.kind` 的 CHECK 写成了 `('series','character')`），`build_meta` 另记 `plot_chunk_char` 一行 —— 共用一个 key 的话后跑那次会覆盖前一次的 rows/built_at |
| [scripts/rescue_moegirl_titles.py](scripts/rescue_moegirl_titles.py) | 补救标题解析漏掉的系列。⚠️ **三步走**：解析（联网）→ 人工过一眼 → `--from-file --apply-b` 应用。中间那步不能省，见 F.3 |
| [scripts/build_char_chunks.py](scripts/build_char_chunks.py) | `plot_chunk` 里 `source='bangumi_char'` 的行 + `plot_chunk_scope` + **`alias` 里 `entity_type='character'` 的行** + `build_meta['char_chunk']`。⚠️ 与 `load_profiles.py` 用**不同的 alias source 值**（`char_*` 前缀），两者永不相交。幂等，md5 跳过 |
| [scripts/extract_char_links.py](scripts/extract_char_links.py) | 阶段 04b。两段：本地提角色页链接 → `--sizes` 联网查 `prop=info`。**不写库**。⚠️ `--sample N` 必须走随机抽样，取 `sorted` 的前 N 个会得到有偏样本（见下） |
| [scripts/fetch_char_pages.py](scripts/fetch_char_pages.py) | 阶段 06 抓角色页 → `data/raw/moegirl_char/*.html.gz` + manifest。**只抓不解析**，幂等、断点续跑。⚠️ 复用 `fetch_moegirl` 的 UA / 7 秒节流 / `resolve_titles`，不另写一份。`--since-year N` 与 `--top` **取并集**（新番热度天然低，交集等于没加）。🚨 **去重必须按 pageid 不能按标题**：54 个 pageid 被 118 个标题指向（《逆转系列/被害人》被 11 个受害者名指向、大小写变体 Death the Kid/kid），按标题去重实测白抓 34 次 |
| [scripts/build_voice_roles.py](scripts/build_voice_roles.py) | `person` / `voice_role` 两表 + `alias` 里 `entity_type='person'` 的行（source 一律 `person_*` 前缀）。⚠️ 与 `load_profiles`（subject 行）、`build_char_chunks`（`char_*`）三者 source 永不相交 |
| [scripts/translate_corpus.py](scripts/translate_corpus.py) | **只写本地缓存 `data/interim/translate_cache/`，不碰数据库**。⚠️ 与灌库分家的理由：翻译很贵（数小时），而灌库策略可能改几次 —— 分开就不用为了改灌库重翻一遍。可中断续跑，自动防系统睡眠 |

📌 **`src/` 下的请求路径模块，都不写库**（线上要用，而 `.vercelignore`
排掉了 `scripts/`）：

| 文件 | 职责 |
|---|---|
| [src/retrieve.py](src/retrieve.py) | ①②③④ 主管道 + G.4 状态机。**只读库** |
| [src/rerank.py](src/rerank.py) | `BAAI/bge-reranker-v2-m3` 客户端。⚠️ **可以换模型**（输出是相对排序，用完即弃），A.8 那条铁律不适用于它 |
| [src/related.py](src/related.py) | 结构化关联查询，读 `staff` / `studios` 两列。**零模型调用** |
| [src/router.py](src/router.py) | 意图分派：`classify()` 判 voice/season/ask，`parse_cour()` 解析时间表达。**纯函数、零模型、不碰库** —— 信号全是触发词与正则 |
| [src/voice.py](src/voice.py) | 声优配役查询（`GET /api/voice`）。**本模块零模型调用**（2026-08-25 起 `POST /api/ask` 的 voice 分支会另外调一次 `llm.voice_answer` 把这里产出的表讲成一段话 —— 那是 main.py 干的，本模块只负责取数与排序）。⚠️ 与 related 的关键差异：**必须从问句里抠人名**，靠 alias 的 `norm_name` 精确匹配（sql/009 把声优灌进 alias 正为此），不做繁简/变体模糊匹配 |

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

# ── 声优配役（零抓取，只读 dump）────────────────────────────
psql < sql/009_voice_role.sql
uv run --group etl python scripts/build_voice_roles.py --dry-run     # 先看账
uv run --group etl python scripts/build_voice_roles.py               # ≈3 分，无 API 调用

# ── 阶段 04b + 06：角色页（唯一一笔大墙钟开销）──────────────
uv run --group etl python scripts/extract_char_links.py              # 本地提链接，~25 秒
uv run --group etl python scripts/extract_char_links.py --sizes --sample 1200
#   ⚠️ 全量 prop=info 约 40 分钟；只为估比例的话用 --sample（**必须随机**）
uv run --group etl python scripts/fetch_char_pages.py --top 500 --dry-run
uv run --group etl python scripts/fetch_char_pages.py --top 500      # ≈10 小时，可断点续跑
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

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

> ✅ 已完工（阶段 06 角色页抓取沿用同一套）。铁律速查：**7 秒/请求勿调低** ·
> UA 诚实（不冒用被封禁 bot 名，联系方式 kevin1035130@outlook.com） ·
> robots 的 `ai-train=no` ⇒ **这份语料永不可用于训练/微调**（embedding 编码不算训练） ·
> CC BY-NC-SA：公开展示正文必须署名 · 抓取与解析两阶段分离（改切分策略不用重抓）。

## F. plot_chunk 语料层 → [docs/corpus.md](docs/corpus.md)

> ✅ 已完工。速查：正文进 Neon（推翻原设计） · **未建 HNSW**，路径③ 579 ms 悬而未决（见 I.5 与 F.1 标注） ·
> 🚨 **2026-08-21 修了两条正在丢内容的解析规则**（见第一部分开工段）：
> songs 分组标题被 12 字下限当残句丢掉（83.7% 的页面受影响）·
> `TABLE_MAX_CELLS` 4→40（**F.4 ④ 的精确根因**：密度判据认得出散文容器，
> 被 cells 硬上限一票否决）。两条都由 `tests/test_parse_moegirl.py` 9 项锁住 ·
> 灌库靠 md5 跳过未变行（否则全表 UPDATE 让 TOAST 翻倍） ·
> **F.5 换机器陷阱**：`moegirl_titles.json` 不入 git，rescue 三条规则要补跑 ·
> `plot_chunk` 的 176 MB 向量在 TOAST 里，要用 `pg_total_relation_size` 看。

## G. 检索层设计（阶段 05）→ [docs/retrieval.md](docs/retrieval.md)

> ✅ 设计依据（实现见 I 节；G.6 两条结论已被 I.2 推翻）。速查：
> LLM `PRIMARY = Qwen/Qwen3-14B` · `FALLBACKS = (GLM-4.5-Air,)`——**fallback 按问答质量选，不按延迟**（G.5f） ·
> HyDE **默认关 —— 已由 20 条查询证实有害**（NDCG 0.638→0.471，p=0.012；
> RRF 也不做，CI 含 0）见 [week5-eval-report.md](docs/week5-eval-report.md) ·
> 召回 50 → rerank → 前 8（G.6） ·
> **rerank 可以换模型，embedding 绝对不行**（输出是相对排序、用完即弃） ·
> 点名查询走 alias 直取，不靠加大 k（G.5g）。

## H. 语料语言统一 → [docs/corpus.md](docs/corpus.md)

> ✅ 已完工（日文残留 0.36% / 0.81%）。速查：管道顺序**剥离 → 换译文 → 切块**，
> 译文缓存的键是剥离后文本（顺序反了 100% 未命中） ·
> ⚠️ `data/interim/translate_cache/`（50 MB）是 4.4 万条译文的**唯一副本**，丢了重翻 8 小时 ·
> 凡改变切块结果的改动都要清孤儿行（判定必须复用 `load_corpus()`，不能另写推导）。

## J. 多轮对话（2026-08-19 加）

> 起因：可回答率打分时 Kevin 发现第 25/30 题触发了反问，**判断反问本身是对的，
> 但「你是指哪一部？」在无状态单轮里是死路** —— 用户选了也没地方送回来。

**无状态**：`scope` 与 `history` 都由调用方传入，服务端零会话存储 ——
与第 2 节「评分随请求传入」同源，游客的 localStorage 与将来的会话表走同一入口。

```
POST /api/ask { question, scope?, history? }
  scope    上一轮反问里用户点中的 series_root → **无条件钉死作用域**（零模型）
  history  最近几轮 (问,答) → 消解「她」「那结局呢」，并在需要时继承作用域
```

**作用域继承的四条规则**（按优先级，全部确定性、零模型）：

| | 条件 | 动作 |
|---|---|---|
| ① | 传了 `scope` | 钉死，不再解析作品名 |
| ② | 本轮 `UNKNOWN` | 继承上一轮作用域 |
| ③ | 本轮 `AMBIGUOUS` 且上一轮作用域**在候选里** | 用它消歧 |
| ④ | 本轮 `AMBIGUOUS` 且候选**全是角色名**（问句没点作品） | 沿用上一轮 |
| — | 其余（含本轮 `OK`） | 用本轮自己的，**不被历史锁死** |

🚨 **③④ 两条是端到端实跑逼出来的，设计时全没料到**：我以为追问句会落在
`UNKNOWN`，实测「三笠是谁」→「她和艾伦是什么关系？」落的是 **`AMBIGUOUS`** ——
而且**进击的巨人根本不在候选里**（它的艾伦存的是「艾伦·耶格尔」，
裸名「艾伦」在 alias 里属于另外 8 部作品）。这正是 I.1 ②「alias 只有官方书写形态、
没有简称」那条缺口在多轮里的形态：**裸名全局有歧义，在上下文里没有。**

🚨 **只继承作用域还不够，还要继承上一轮的 `character_ids` 做 ① 直取。**
实测继承作用域后召回了 6 条 chunk，**LLM 仍答「资料中没有提到」** ——
因为送去做向量召回的查询串里还留着代词「她」。而上一轮已经确定性地知道
「她」= 三笠，**直取不经过向量、不受代词影响**。补上之后答对。

⚠️ **上下文长度必须有硬上限**（`MAX_HISTORY_TURNS=3` · `MAX_HISTORY_CHARS=300`）：
I.2 ② 实测低分 chunk 会稀释上下文把 LLM 逼成拒答，**历史是同一种噪声，
而且不受 `MIN_SCORE` 地板约束**（地板管 chunk，管不到历史）。
⚠️ 历史以 **messages 形式**给，**不拼进「资料」里** —— 拼进去会架空
`ANSWER_SYSTEM` 第 1 条，让上一轮的回答变成"资料"，错误会在多轮里自我强化。

🚨 **顺带修掉一个反问的 bug**：候选原先**按标题去重**，于是
《多罗罗》1969（done=911）与 2019（done=9450）折成一个选项，
「你是指哪一部？《多罗罗》」**用户无从选择**。全库有 **81 个**同名标题
（忍者神龟×3 铁臂阿童木×3 狮子王×3 Kanon×2…），正是 E.3 记过的
「重制/不同改编同名」那批。⇒ 改为按 `series_root` 去重 + 同名时补年份。

⚠️ **对评测口径无影响**：`scope`/`history` 默认为空，单轮行为逐字节不变，
所以 60 题打分表的结果仍然有效。

## I. 检索层实现（阶段 05）→ [docs/retrieval.md](docs/retrieval.md)

> ✅ 已完工（`/api/ask` · `/api/related`）。速查：推翻 G 节两条 ——
> `PIN_RESERVE` 保底席位（alias 直取的 chunk 不能被 rerank 挤掉）+ `MIN_SCORE` 相关度地板
> （低分 chunk 稀释上下文会把 LLM 逼成拒答；pinned 豁免地板）（I.2） ·
> G.4 四种状态一律返回 200，失败按层分开：embedding 挂 503 / rerank 挂降级 200 / LLM 挂 503（I.6） ·
> **`SONGS_SEAT` songs 保底席位**（2026-08-20）：OP/ED 问句给 songs 层第 1 一个
> **独立于 `PIN_RESERVE` 的席位**，`0/7 → 5/7` 而上下文只 +0.09 条/题 ——
> ⚠️ 占座不是豁免地板，⚠️ rerank 分噪声 ~1e-3 所以绝对地板调不稳，见报告 §5 ·
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
- ~~📌 **前端刻意冻结在 v0**：先把逻辑全部做完，最后一次性写前端。~~
  🎉 **已解冻并写完（2026-08-24/25）** —— 这条纪律的目的（不在逻辑没定型时
  反复返工）**已经达成**，不是被推翻。前端现状见第一部分「前端现状」那一节。
  docs/history.md 第 2 周记的欠账清单（搜索/详情页/多次作答/experience/mode）
  除「详情页」外均已实现。

---

# 第四部分 · 待办（按优先级）

### ⬜ 英文提问支持 —— 上面那条修完之后剩下的三个缺口

📌 修完词边界后英文实测 `resolve()` 从 **6/6 全灭** 变成 4 条正确
（Steins;Gate / Attack on Titan / Mikasa Ackerman / ending of Attack on Titan）。
剩下三件事，**建议一起决定，不要零敲碎打**：

**① ✅ 路由层的 season 分支已认英文（2026-08-22）。**
`_EN_SEASON_TRIGGERS` + 英文时间正则，24 项测试。零模型、零延迟。
```
What anime were people watching ten years ago today?
  → season · 2016 年 7 月番共 134 部（你的名字。/ 灵能百分百 / 声之形）
```
覆盖：`July 2016` · `summer of 2016` · `from 2016` · `ten years ago` ·
`10 years ago` · `a decade ago` · `last/next year` · `this/next season`。

⚠️ **英文单独一份表，不要把中文那份翻译过去。** 中文的「番」「季度」
几乎只用于档期语境；英文的 season **压倒性地指「某部作品的第几季」**。
🚨 **分界线是介词**：`next season` 是档期，`next season **of** X` 是续作，
`last season **in** Attack on Titan` 也是续作 ⇒ 用否定预查
`(?!\s+(?:of|in|for)\b)` 挡掉。⚠️ 而 `classify()` 是**纯函数、零 DB**
（模块注释里的设计约束），没法靠「句里有作品名」消歧，介词是唯一可用的
确定性信号。⚠️ 裸的 `last season` **有意不收**（即使有护栏）——
它的续作义太强，而 `what aired last season` 这种日历义写法远没那么常见。
⚠️ 英文匹配一律带 `\b` 词边界：否则 `may` 命中 `maybe`、`fall` 命中
`fallen` —— 与 `retrieve._latin_word_boundary` 是同一类问题的同一个解法。

⬜ **剩下两条分支仍不认英文**：
  - **voice**：`voice.wants()` 的触发词全是中文。⚠️ 而且**加了也只能用一半** ——
    实测英文声优名只有**日式语序**的罗马字在库里
    （`hanazawakana` ✅ / `kanahanazawa` ❌ / `kuginorie` ❌），
    这是覆盖问题，词表修不了。且 `voice.find_person()` **没有词边界保护**，
    直接放英文进去会撞出假阳性（缺口 ② 的形态）。
  - **ask**：`resolve()` 已经能认英文作品名（见上一条修复），但
    `route_reason` / `answer` 仍是中文 —— 英文 UI 是另一件事，见下。
  - 让模型抽结构。⚠️ 若走这条，**抽「可验证的结构」不要抽「意图标签」**：
    抽出的作品名要拿去 alias 里验，验不上就回落；意图标签没法验，
    错了是静默的。这才是与第 15 节原则 2 不冲突的用法。

**② 短拉丁别名撞英文虚词**（`GENERIC_NAMES` 的英文形态）：
```
'...watching ten years ago...' → ten → 福星小子 · ago → 虚拟小姐在看着你
'...as a Slime'               → asa ← "as"+"a" 拼出来的，原文不是一个词
```
实测 25 个常见英文虚词 **8 个撞上别名**，全在 3 字符档；2–3 字符纯拉丁
别名共 1,029 行（占拉丁别名 1.8%）。
⚠️ **必须语义列举，不能按「撞几部」自动筛** —— 与泛称那条同一个坑。
📌 **现在零影响**（纯中文问句不含拉丁，触发不到），所以不急。

**③ `MENTION_MAX = 16` 卡住长英文标题。** `fullmetalalchemist` 是 18 字符，
子串枚举不出来 ⇒ 仍是 UNKNOWN。这个常量按中文标题标定（中文标题字符数
天然短）。放宽到 ~24 的代价只是子串变多（SQL 是 `= ANY` 索引查找），
但**会同时影响中文路径**，要单独测一轮再动。

⚠️ **在动 ①② 之前先想清楚「要不要入站翻译」** —— 若决定把英文问句翻成
中文再走管道，路由层就跑在译文上，① 自动消失。但翻译的收益**必须先测**：
它的形状与 HyDE 一模一样（每次请求多一次确定的 LLM 调用换未测量的收益），
而 HyDE 实测是 NDCG 0.638 → 0.471（p=0.012）被否决的。
💡 现成的实验很便宜：150 题自动标注评测集翻成英文重跑 = 干净 A/B。
⚠️ 且**实体链接不该靠翻译**：alias 里本来就有 8,931 行拉丁 subject 别名，
直接匹配零成本零延迟，比 MT 猜标题准（`Fullmetal Alchemist Brotherhood`
译成什么中文是不确定的）——与 G.5g「点名走 alias 直取」同一条。

### ⬜ 同 IP 衍生折叠 —— `/api/related` 32% 的结果是噪声

问灵能百分百会返回它自己的「REIGEN」「第一回灵能相谈所」「10周年纪念映像」。
根因是 `series_root` 只折叠 `rt=2/3`（前传/续集），没折叠 `rt=6/11/12`（番外/衍生）。
**完整数据与两个修法的取舍见 I.3 末尾。**

📌 **2026-08-21 量化了这个缺口，比原先以为的大**：全库 5,958 部作品自成一根
且无萌娘语料，其中 **703 部的标题以某个「有语料的根」为前缀**，
合计占全库热度 **4.95%** —— 它们的语料其实就在库里，只是作用域映射不过去。
触发案例：《总之就是非常可爱 ～制服～》(root=376708) 拿不到主系列
《总之就是非常可爱》(root=301541) 的萌娘页，而 B.1 有一道结局题正好问它。
⇒ **「覆盖作品 79.3% / 热度加权 97.3%」是低估的。**

🚨 **但这批数据同时证伪了方案 A（标题前缀）**：热度最高的那个是
「钢之炼金术师 FULLMETAL ALCHEMIST」(done=37,474) —— 它是 2009 重制版，
**后半剧情与 2003 版完全不同**，蹭 2003 版的语料会答错。
正是 E.3 记的「重制/不同改编同名」那 81 个同名标题的坑。
⇒ **方案 B（载入 dump 的 `subject-relations`，按 `rt` 判断）才是正解**，
标题前缀只能当临时补丁。留到第 6 周与那条 `rt=12` 遗留缺口一起解决。
⚠️ 它要动 `series_root`，会牵连推荐的续作折叠、`/api/related`、问卷选题三处，
**不要和语料改动混在一次做**。

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

### ⬜ `summary` 噪声 —— 🔓 **前置已解除（第 5 周已收口）**

自检实测：**1.0% 短于 20 字**（「18X版」「TV二期。」这类）· **0.5% 含 URL** ·
**128 部共用重复 summary**。

~~现在清洗会污染第 5 周 baseline 的口径~~ —— **baseline 已跑完（2026-08-23）**，
限制解除。⚠️ 但动手时仍要注意它**会改变 `vec` 的非空集合**，属于重编码级改动，
应作为一个 ablation 变量单独处理，不要顺手改。

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

### ⬜ 推荐结果要加热度权重 —— 🔓 **前置已解除（第 5 周已收口）**

已确认需要：纯 tag 余弦没有热度先验，冷门作品只要那 3–4 维恰好对上就能拿高分。
实测「硬核科幻」档案的 Top5 里出现 `宇宙战舰大和号 新的旅程`(done=105)、
`永远的大和号`(done=92) 这类几乎无人看过的作品。

~~不能现在加~~：第 10 节把「热度」列为四条 baseline 之一，混进去就分不清
NDCG 的提升来自 tag 模型还是热度先验。**但 baseline 已于 2026-08-23 跑完**
（第 5 周五项收口），这条限制到此解除，可以开工。

（2026-08-11 决定：Kevin 认为热度权重有必要，但同意排在 baseline 之后。）

### ⬜ `轻小说改` 与 `小说改` 高度共现，等于同一信号算两遍（第 3 周 P1 时看）

推荐结果里这两个 tag 几乎每条都同时出现。第 13 节明确写了两者**不合并**
（一般小说 vs 轻小说，受众不同），这条不变；但**共现**没处理 ——
一部轻改作品在向量里会同时点亮两维，相当于把「轻改」这个信号加权了两次，
放大了轻改类作品之间的相似度。

同类嫌疑还有 `异世界`+`穿越`、`后宫`+`校园`。做法是统计全库 tag 共现矩阵，
看哪些对的 PMI 高到该做降权。~~现在不动，会污染 baseline 口径~~ ——
**baseline 已于 2026-08-23 跑完**，限制解除。

### ⬜ 「未开播」档的档期数据太稀疏（第 6 周季度同步时解）

`--mode upcoming` 实测只召回 2 部。dump 是静态快照，对未来季度天然滞后，
而「这季看什么」最需要的恰恰是还没播的。要靠第 6 节的季度增量同步
（走 Bangumi API 而非 dump）补齐。

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

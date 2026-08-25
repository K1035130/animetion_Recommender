# animetion_Recommender

An anime recommendation service with a built-in question-answering assistant.

Users rate the shows they have already seen. From those ratings the system builds
a profile of their taste and predicts how much they are likely to enjoy every
other title in the library. A second, independent feature answers questions about
plots, voice actors and broadcast seasons, or finds a show from a description —
always citing the source text it drew on.

**Live:** [animetion-recommender.vercel.app](https://animetion-recommender.vercel.app/)
— the web interface and the API are served from a single Vercel project.

> Design decisions, measurements and open questions are recorded in
> [CLAUDE.md](CLAUDE.md) (Chinese). This document covers current status and setup.

---

## Overview

**Recommendation(ML).** A short questionnaire produces a preference vector — a
numeric summary of the user's taste — which is then compared against every title
in the library. Three signals are combined: 308 curated genre tags, an *embedding*
of each synopsis (a numeric representation of text, arranged so that similar
descriptions sit close together), and staff and studio credits. No machine-learning
model runs while a request is being served; all vectors are computed in advance,
so a recommendation is arithmetic over stored numbers.

**Question answering(RAG).** A single input box handles four kinds of question — plot,
voice-actor filmography, broadcast season, and finding a show from a description.
The request is dispatched by deterministic rules and then confirmed by one
language-model call, which catches cases where the rules guessed wrong. Plot
answers are grounded: the system retrieves passages from a Chinese-language corpus
and instructs the model to answer only from them.

**Accounts.** Sign-up uses a username rather than an email address, because the
service has no way to send mail and therefore could not offer password recovery.
Passwords are hashed with argon2; sessions are JSON Web Tokens held in an
httpOnly cookie. Ratings sync to the account, and question answering is limited to
ten questions per 24 hours.

### Scale

| | |
|---|---|
| 11,453 | titles in the candidate set, defined in `src/candidates.py` |
| 132,056 | corpus passages, covering 79.3% of titles (97.3% weighted by popularity) |
| 263,690 | aliases across titles, characters and people |
| 145,306 | voice-acting credits, covering 8,215 voice actors |
| 19,381 | passages flagged as spoilers and filtered before retrieval |
| 1,001 MB | Neon Postgres database |
| 329 | automated tests |

---

## Status

| Stage | Content | State |
|---|---|---|
| 1–3 | Data layer, scoring, questionnaire, deployment, embeddings, signal fusion | Complete |
| 4 | Corpus collection, translation to Chinese, retrieval pipeline | Complete |
| 5 | Offline evaluation | Complete |
| 5.5 | Parser corrections, character pages, voice-acting data | Complete |
| 6 | Accounts and web interface complete; question selection and quarterly sync outstanding | In progress |

### Planned work

Six items, none of which blocks the others.

| Item | Current position |
|---|---|
| **Quarterly update** | Refreshing existing titles is close to free: the pipeline is idempotent and skips unchanged rows. The obstacle is admission. New shows are excluded by the `favorite.done >= 50` popularity threshold that guards data quality. The library's latest broadcast date is 2026-09-11, only two titles are scheduled in the future, and all six titles listed for 2027 or later carry no broadcast date at all — so any "airing within N months" rule must handle the missing value. The admission rule and the tag vocabulary policy are decisions to settle before writing code. |
| **Information-gain question selection** | Questions are currently selected for diversity. Selecting instead by expected information gain would make a fixed number of answers more informative. |
| **Detail page** | The endpoint `GET /api/anime/{id}` has been available since stage 2; no page in the web interface calls it. |
| **English-language support** | Broadcast-season queries already accept English. Three gaps remain: voice-actor phrasing is recognised only in Chinese; the maximum recognised title length is shorter than some English titles; and 14 of 33 common English words collide with genuine aliases, so an English description resolves to an unintended title. Because the corpus is Chinese, the cross-language retrieval penalty should be measured before an approach is chosen; translating the existing 150-question evaluation set provides that measurement at almost no cost. |
| **Episode synopses** | The source archive contains 108,835 episode descriptions across 10,630 titles, none of which are loaded. They would answer per-episode questions, which currently fail. |
| **Voice actors as a recommendation signal** | The staff vector covers directors, composers and studios but no voice actors, which require a two-step join through character records. The data is already present from the question-answering feature. Adding it changes the vocabulary size and therefore requires recomputing the library. |

---

## Architecture

| Layer | Choice |
|---|---|
| Data processing | Python 3.12, orjson, [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| Database | Neon Postgres 18.4 with pgvector 0.8.1 |
| Chinese tokenization | jieba, applied before insertion — the database cannot install a Chinese tokenizer |
| Embeddings | Qwen3-Embedding-0.6B, stored as `halfvec(1024)` |
| Reranking | `BAAI/bge-reranker-v2-m3`, which reorders a retrieved shortlist more accurately than the initial search |
| Generation | Qwen3-14B for answers; Qwen3-8B for intent classification and phrasing |
| Authentication | argon2id with JSON Web Tokens in an httpOnly cookie |
| Backend | FastAPI on Vercel serverless functions |
| Frontend | React, TypeScript, Vite and Tailwind |

**The embedding model is fixed and cannot be substituted.** Its output is a set of
coordinates that is only meaningful relative to the vectors already stored. If a
second encoder were introduced, the resulting comparisons would be meaningless,
yet the system would return a correctly sorted, entirely plausible list and raise
no error. The language model and the reranker carry no such constraint: their
output is text or an ordering, used once and discarded. Should the embedding
service become unavailable, the correct fallback is keyword search, not a
different encoder.

---

## Getting started

### 1. Install

```bash
uv sync                       # runtime dependencies only — what the deployed function needs
uv sync --group etl           # adds bgm-tv-wiki, tqdm, lxml — required by scripts/
uv sync --group api --group etl --group dev   # full development environment
```

The main dependency group serves as the deployment manifest. Vercel's Python
runtime reads `pyproject.toml` and `uv.lock` and installs that group; a
`requirements.txt` file is ignored. Any package used while serving a request must
be in the main group, or the application fails at import time in production.
Packages used only by data-processing scripts must stay in a named group, or they
are bundled into the deployed function.

### 2. Configure

```bash
cp .env.example .env
```

| Variable | Purpose |
|---|---|
| `DATABASE_URL_DIRECT` | Schema changes and bulk loading. Uses a direct connection, avoiding a conflict between the connection pooler and prepared statements. |
| `DATABASE_URL` | The deployed application; uses the pooler. Optional locally. |
| `SILICONFLOW_API_KEY` | Embeddings, reranking and generation share a single key. |
| `AUTH_SECRET` | Session signing, minimum 32 characters. The application refuses to start without it rather than falling back to a default, which would let a misconfigured deployment issue valid-looking tokens signed with a publicly known key. |
| `CORS_ORIGINS` | Local development only. Production is same-origin, so the middleware never runs. |

Deployment requires exactly three: `DATABASE_URL`, `SILICONFLOW_API_KEY` and
`AUTH_SECRET`. `DATABASE_URL_DIRECT` should not be set in production, because the
connection pool resolves `DATABASE_URL or DATABASE_URL_DIRECT` — with both
present, a typing error in the first falls back silently to a direct connection:
the service starts, appears healthy, and gradually exhausts the database's
connection limit. Each variable has an independent check: `/api/health` returning
a catalogue size confirms the database, registering an account confirms
`AUTH_SECRET`, and asking a plot question confirms the API key.

### 3. Obtain the source data

```bash
# The current archive URL and its checksum come from aux/latest.json
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json
mkdir -p data/raw && curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

Approximately 410 MB compressed and 1.8 GB extracted.

### 4. Build the database

The order matters.

```bash
uv sync --group etl
for f in sql/0*.sql; do psql < "$f"; done   # tables, corpus, voice roles, accounts

uv run python scripts/build_id_map.py        # network access required
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # network access, roughly 125 requests
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py
uv run python scripts/build_embeddings.py    # requires SILICONFLOW_API_KEY; about 12 minutes
uv run python scripts/build_staff_vectors.py
uv run --group ml python scripts/build_clusters.py
uv run --group etl python scripts/build_voice_roles.py   # about 3 minutes, no API calls

# Corpus collection — the one substantial time cost
uv run --group etl python scripts/fetch_moegirl.py       # about 4 hours; the 7-second
                                                         # request interval is deliberate
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/build_char_chunks.py
uv run --group etl python scripts/extract_char_links.py --sizes
uv run --group etl python scripts/fetch_char_pages.py --top 1000   # about 10 hours, resumable
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

psql -c 'VACUUM FULL anime_profile'
uv run --group etl python -m pytest tests/ -q
```

Every script is idempotent and safe to re-run. The final two steps are not
optional. Skipping the vacuum roughly doubles the apparent size of the database,
and skipping the tests leaves a missing tag vector undetected — in which case
scoring returns an empty list without raising an error. The `/api/health` endpoint
reports the same coverage figure for this reason.

Run the test suite with `--group etl`. Twenty-eight tests depend on `lxml` and are
skipped rather than failed when it is absent, so a passing run may simply mean
they never executed; check the summary line for skipped tests.

### 5. Run

```bash
uv run uvicorn server.main:app --reload      # API, documentation at /api/docs
cd web && npm install && npm run dev         # web interface on port 5173
uv run --group etl python -m pytest tests/ -q
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint
```

On Windows, prefix Python commands with `PYTHONIOENCODING=utf-8` for scripts that
print Chinese text to the console.

---

## API

All routes are served under `/api`.

The recommendation path is stateless: ratings are supplied with each request and
nothing is written to the server. Anonymous visitors and signed-in users enter
through the same code path, and the account layer sits alongside it rather than
within it.

| Endpoint | Purpose | Model calls |
|---|---|---|
| `GET /health` | Liveness, data-coverage figures and tokenizer fingerprint | — |
| `GET /questionnaire` | Select questions | — |
| `POST /recommend` | Score the library against a set of ratings | — |
| `GET /search` | Search by title or alias, with a fuzzy fallback for misspellings | — |
| `GET /anime/{id}` | Title detail | — |
| `GET /season` | Browse a broadcast season | — |
| `GET /related` | Other works by the same author, director or studio | — |
| `GET /voice` | Roles played by a given voice actor | — |
| `GET /find` | Find a title from a free-text description | 1 embedding |
| `POST /ask` | Single entry point; dispatches to the four question types | 2 or more |

Account routes: `POST /auth/register`, `/auth/login`, `/auth/logout`;
`GET /auth/me`; `PUT /auth/username`, `/auth/password`;
`GET`, `PUT`, `DELETE /ratings`; `GET /ratings/detail`.

The boundary between anonymous and authenticated access is determined by cost,
not by resemblance to question answering. `/voice` and `/season` are ordinary
database queries and remain open. `/find` makes a single embedding call and
requires an account, because leaving it open would provide a cost-free route
around the quota.

Clients submit the user's **choice**, not a computed score:

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

The mapping from choice to score and confidence is defined once, on the server.
Computing it in the client would duplicate the mapping into TypeScript, where any
divergence would degrade recommendations silently. The same reasoning is why the
database stores the choice rather than the derived score.

---

## Project layout

```
src/
  candidates.py     Candidate-set criteria — the single source of truth
  tag_rules.py      Tag classification, synonym mapping, import-time self-check
  tagvec.py         The one implementation of the tag vector
  recommend.py      In-memory scoring, used by the offline evaluation
  recommend_sql.py  Database scoring, used online; must remain equivalent
  questionnaire.py  Question selection and answer-to-rating mapping
  textproc.py       Tokenization and dictionary fingerprinting
  embed.py          The sole definition of the embedding model
  retrieve.py       Retrieval pipeline and entity resolution; read-only
  rerank.py         Reranker client
  llm.py            Generation, intent classification, prompts, configuration fingerprint
  router.py         Intent dispatch — pure functions, no model, no database access
  find.py, voice.py, related.py    Show-finding, casting, related works
  auth.py, ratings.py, quota.py    Accounts, rating sync, question quota
server/             FastAPI application
web/                React frontend
  session.tsx       The single entry point for session state and ratings
api/index.py        Vercel entry point; this directory must contain nothing else
sql/ scripts/ tests/
```

`web/src/session.tsx` is the first file to read on the frontend. Components above
it are unaware of whether a user is signed in; they receive a ratings object and
a setter, and that module decides whether a rating is stored locally or synced to
an account. It is the frontend counterpart of the server-side rule that ratings
travel with the request. If a component begins testing for a signed-in user
itself, the rule is broken in several places at once, each diverging separately.

---

## Dataset criteria

```
type == 2                                    # anime
AND has a broadcast year                     # falls back to the infobox when the date is empty
AND meta_tags ∩ {TV, WEB, Movie, OVA} != ∅   # excludes shorts and untagged fan works
AND favorite.done >= 50                      # quality threshold
→ 11,453 titles
```

Defined in [src/candidates.py](src/candidates.py); the criteria should be changed
only in that file. The threshold of 50 completed viewings removes three
data-quality problems simultaneously: titles with no tags, no rating, and no
audience. It is also the reason new shows cannot enter the library, which is the
open decision behind the quarterly update described above.

---

## License

See [LICENSE](LICENSE).

---
---

# animetion_Recommender（中文）

一个动画推荐服务，并内置了问答助手。

用户为看过的作品评分，系统据此建立口味画像，预测他对库中其余每一部作品的
喜好程度。另一条独立的功能线负责回答剧情、声优、播出档期方面的问题，
或根据一段描述找出对应的作品，并且始终标明依据的原文出处。

**已上线：**[animetion-recommender.vercel.app](https://animetion-recommender.vercel.app/)
—— 网页界面与 API 由同一个 Vercel 项目提供。

> 设计决策、实测数据与未决问题记录在 [CLAUDE.md](CLAUDE.md)。本文档说明当前状态与部署方式。

---

## 功能概览

**推荐(ML)** 一份简短的问卷会生成偏好向量 —— 用户口味的数值化描述 ——
再拿它与库中每一部作品比对。三种信号被融合在一起：308 个经过清洗的题材标签、
每部作品简介的 *embedding*（文本的数值表示，使意思相近的描述在数值上彼此靠近），
以及制作人员与公司信息。**处理请求的过程中不运行任何模型**：
所有向量都已预先算好，一次推荐只是对存储数值做算术。

**问答(RAG)** 一个输入框处理四类问题 —— 剧情、声优配役、播出档期，
以及根据描述找番。请求先由确定性的规则分派，再由一次语言模型调用复核，
以拦下规则判断失误的情况。剧情类回答是有依据的：系统先从中文语料中检索段落，
再要求模型只依据这些段落作答。

**账号。** 注册使用用户名而非邮箱，因为本服务没有发信能力，也就无法提供密码找回。
密码以 argon2 哈希存储，会话是放在 httpOnly cookie 中的 JSON Web Token。
评分会同步到账号，问答限制为每 24 小时 10 条。

### 规模

| | |
|---|---|
| 11,453 | 候选集中的作品数，口径定义在 `src/candidates.py` |
| 132,056 | 语料段落，覆盖 79.3% 的作品（按热度加权为 97.3%） |
| 263,690 | 别名，涵盖作品、角色与人物 |
| 145,306 | 声优配役记录，涉及 8,215 位声优 |
| 19,381 | 被标记为剧透并在检索前过滤的段落 |
| 1,001 MB | Neon Postgres 数据库 |
| 329 | 自动化测试 |

---

## 当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1–3 | 数据层、打分、问卷、部署、embedding、信号融合 | 已完成 |
| 4 | 语料采集、统一译为中文、检索管道 | 已完成 |
| 5 | 离线评测 | 已完成 |
| 5.5 | 解析器修正、角色页、声优数据 | 已完成 |
| 6 | 账号与网页界面已完成；选题优化与季度同步尚未开始 | 进行中 |

### 计划中的工作

六项，互不阻塞。

| 事项 | 目前进展 |
|---|---|
| **季度更新** | 更新已有作品的成本接近于零：管道幂等，且会跳过未变化的记录。障碍在准入。新番会被 `favorite.done >= 50` 这条保障数据质量的热度门槛排除在外。库中最晚的播出日期是 2026-09-11，未来待播的只有两部，而所有标记为 2027 年及以后的六部作品**都没有播出日期** —— 因此任何「未来 N 个月内开播」的规则都必须处理缺失值。准入规则与标签词表策略是动手之前要先定下的两个决策。 |
| **信息增益选题** | 目前按多样性选题。改为按预期信息增益选题，可以让同样数量的作答提供更多信息。 |
| **详情页** | `GET /api/anime/{id}` 自阶段 2 起即可用，但网页界面中没有任何页面调用它。 |
| **英文支持** | 档期查询已支持英文。仍有三处缺口：声优类问法只能识别中文；可识别的标题长度上限短于部分英文标题；33 个常见英文词中有 14 个与真实别名冲突，导致英文描述被解析成非预期的作品。由于语料是中文的，应先度量跨语言检索的损失再选择方案 —— 把现有的 150 题评测集译成英文即可得到这一度量，成本极低。 |
| **单集简介** | 源数据中含有 108,835 条分集简介，涉及 10,630 部作品，目前一条都未导入。它们可以回答按集提问的问题，而这类问题目前无法回答。 |
| **声优作为推荐信号** | 制作人员向量涵盖导演、音乐与制作公司，但不含声优 —— 声优需要经由角色记录做两步关联。数据已因问答功能而具备。引入它会改变词表规模，因而需要重算全库。 |

---

## 技术架构

| 层 | 选型 |
|---|---|
| 数据处理 | Python 3.12、orjson、[bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1 |
| 中文分词 | jieba，在入库前完成 —— 该托管数据库无法安装中文分词扩展 |
| Embedding | Qwen3-Embedding-0.6B，以 `halfvec(1024)` 存储 |
| 重排 | `BAAI/bge-reranker-v2-m3`，对检索出的候选列表做比初检更精确的重新排序 |
| 生成 | Qwen3-14B 负责回答；Qwen3-8B 负责意图分类与文案组织 |
| 认证 | argon2id，JSON Web Token 存于 httpOnly cookie |
| 后端 | FastAPI，运行于 Vercel serverless 函数 |
| 前端 | React、TypeScript、Vite、Tailwind |

**embedding 模型是锁定的，不可替换。** 它的输出是一组坐标，
只有相对于库中已有向量才有意义。一旦引入第二个编码器，比对结果将失去意义，
而系统仍会返回一个排序正确、看起来完全合理的列表，并且不报任何错误。
语言模型与重排模型没有这个约束：它们的输出是文本或排序，用完即弃。
若 embedding 服务不可用，正确的降级方向是关键词检索，而不是换一个编码器。

---

## 快速开始

### 1. 安装

```bash
uv sync                       # 仅运行时依赖 —— 线上函数所需的就是这一组
uv sync --group etl           # 追加 bgm-tv-wiki、tqdm、lxml —— scripts/ 需要
uv sync --group api --group etl --group dev   # 完整开发环境
```

主依赖组同时充当部署清单。Vercel 的 Python 运行时读取 `pyproject.toml`
与 `uv.lock` 并安装该组，`requirements.txt` 会被忽略。
凡是在处理请求时用到的包都必须放在主依赖组中，否则线上会在导入阶段直接失败；
仅供数据处理脚本使用的包则必须留在具名分组内，否则会被打包进部署产物。

### 2. 配置

```bash
cp .env.example .env
```

| 变量 | 用途 |
|---|---|
| `DATABASE_URL_DIRECT` | 建表与批量灌数据。走直连，以避开连接池与预编译语句之间的冲突。 |
| `DATABASE_URL` | 线上应用使用，走连接池。本地可不填。 |
| `SILICONFLOW_API_KEY` | embedding、重排与生成共用同一个密钥。 |
| `AUTH_SECRET` | 会话签名，至少 32 字符。缺失时应用会拒绝启动而非退回默认值 —— 若有默认值，配置遗漏的部署会签发出看似有效、实则使用公开密钥的令牌。 |
| `CORS_ORIGINS` | 仅本地开发使用。线上为同源，该中间件不参与。 |

部署只需要其中三个：`DATABASE_URL`、`SILICONFLOW_API_KEY` 与 `AUTH_SECRET`。
`DATABASE_URL_DIRECT` 不应配置到线上。连接池按
`DATABASE_URL or DATABASE_URL_DIRECT` 的顺序取值，因此两者都配置时，
前者一旦拼写出错就会**静默退回直连**：服务照常启动、状态看似正常，
却在逐步耗尽数据库的连接数。三个变量各有独立的验证方式 ——
`/api/health` 返回作品总数说明数据库连通，注册账号说明 `AUTH_SECRET` 生效，
提出一个剧情问题说明 API 密钥可用。

### 3. 获取源数据

```bash
# 当前压缩包地址与校验值来自 aux/latest.json
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json
mkdir -p data/raw && curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

压缩包约 410 MB，解压后约 1.8 GB。

### 4. 建库

顺序不能调换。

```bash
uv sync --group etl
for f in sql/0*.sql; do psql < "$f"; done   # 建表、语料、声优、账号

uv run python scripts/build_id_map.py        # 需要联网
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # 需要联网，约 125 次请求
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py
uv run python scripts/build_embeddings.py    # 需要 SILICONFLOW_API_KEY；约 12 分钟
uv run python scripts/build_staff_vectors.py
uv run --group ml python scripts/build_clusters.py
uv run --group etl python scripts/build_voice_roles.py   # 约 3 分钟，无 API 调用

# 语料采集 —— 唯一一笔可观的时间开销
uv run --group etl python scripts/fetch_moegirl.py       # 约 4 小时；7 秒的请求间隔
                                                         # 是有意设置的，不要调低
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/build_char_chunks.py
uv run --group etl python scripts/extract_char_links.py --sizes
uv run --group etl python scripts/fetch_char_pages.py --top 1000   # 约 10 小时，可断点续跑
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

psql -c 'VACUUM FULL anime_profile'
uv run --group etl python -m pytest tests/ -q
```

所有脚本均幂等，可安全重复执行。最后两步不是可选的：跳过 vacuum 会使数据库
表观体积增加近一倍；跳过测试则会让标签向量缺失的问题无人察觉 ——
而在那种情况下，打分会**返回空列表且不报错**。`/api/health` 输出同一项覆盖率
数据也正是出于这个原因。

运行测试时要带 `--group etl`。其中 28 项测试依赖 `lxml`，缺失时会被**跳过而非失败**，
因此「全部通过」有可能只是它们从未执行；请检查结果中的 skipped 计数。

### 5. 运行

```bash
uv run uvicorn server.main:app --reload      # API，文档位于 /api/docs
cd web && npm install && npm run dev         # 网页界面，端口 5173
uv run --group etl python -m pytest tests/ -q
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint
```

在 Windows 上，向控制台输出中文的脚本需加前缀 `PYTHONIOENCODING=utf-8`。

---

## 接口

所有路由均位于 `/api` 之下。

推荐链路是无状态的：评分随每次请求传入，服务端不做任何写入。
匿名访客与登录用户走同一条代码路径，账号层**并列**于其旁而非嵌入其中。

| 接口 | 用途 | 模型调用 |
|---|---|---|
| `GET /health` | 存活探针、数据覆盖率与分词器指纹 | — |
| `GET /questionnaire` | 选题 | — |
| `POST /recommend` | 依据一组评分对全库打分 | — |
| `GET /search` | 按标题或别名检索，并对拼写错误提供模糊兜底 | — |
| `GET /anime/{id}` | 作品详情 | — |
| `GET /season` | 浏览某一播出档期 | — |
| `GET /related` | 同一作者、导演或公司的其他作品 | — |
| `GET /voice` | 某位声优出演过的角色 | — |
| `GET /find` | 依据一段自由描述查找作品 | 1 次 embedding |
| `POST /ask` | 单一入口，分派至四类问题 | 2 次及以上 |

账号相关：`POST /auth/register`、`/auth/login`、`/auth/logout`；
`GET /auth/me`；`PUT /auth/username`、`/auth/password`；
`GET`、`PUT`、`DELETE /ratings`；`GET /ratings/detail`。

匿名与登录的边界由**成本**决定，而非由「是否像问答」决定。
`/voice` 与 `/season` 是普通的数据库查询，保持开放；
`/find` 会产生一次 embedding 调用，因而要求登录 ——
若对其开放，它就成了绕过配额的零成本通道。

前端提交用户的**作答选项**，而非算好的分数：

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

选项到分数与置信度的映射只在服务端定义一次。若改由前端计算，
这份映射会被复制进 TypeScript，而一旦两处出现分歧，
推荐质量的下降将是静默的。数据库存储作答选项而非派生分数，出于同一考虑。

---

## 目录结构

```
src/
  candidates.py     候选集口径 —— 唯一事实来源
  tag_rules.py      标签分类、同义合并、导入时自检
  tagvec.py         标签向量的唯一实现
  recommend.py      内存打分，供离线评测使用
  recommend_sql.py  数据库打分，线上使用；必须与上者保持等价
  questionnaire.py  选题与作答到评分的映射
  textproc.py       分词与词典指纹
  embed.py          embedding 模型的唯一定义处
  retrieve.py       检索管道与实体解析；只读
  rerank.py         重排客户端
  llm.py            生成、意图分类、提示词、配置指纹
  router.py         意图分派 —— 纯函数，不调模型、不访问数据库
  find.py、voice.py、related.py    找番、配役、关联查询
  auth.py、ratings.py、quota.py    账号、评分同步、问答配额
server/             FastAPI 应用
web/                React 前端
  session.tsx       会话状态与评分的单一入口
api/index.py        Vercel 入口；该目录不得放置其他文件
sql/ scripts/ tests/
```

`web/src/session.tsx` 是前端首先应当阅读的文件。它上层的组件并不知道用户是否已登录，
它们拿到的只是评分对象与写入函数，而由该模块决定评分是存于本地还是同步到账号。
这是服务端「评分随请求传入」这条规则在前端的对应物。
一旦某个组件自行判断登录状态，这条规则就会在多处同时被打破，且各自独立地漂移。

---

## 数据集口径

```
type == 2                                    # 动画
AND 有播出年份                                # 日期为空时回退至 infobox
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除短片与无形态标签的同人作品
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

口径定义在 [src/candidates.py](src/candidates.py)，**修改口径只应改动该文件**。
「看过人数不少于 50」这一门槛同时消除了三类数据质量问题：无标签、无评分、无观众。
它也是新番无法进入库中的原因，正是上文季度更新所对应的待决问题。

---

## License

参见 [LICENSE](LICENSE)。

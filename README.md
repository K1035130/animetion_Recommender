# animetion_Recommender

A preference-questionnaire-driven anime recommender. Users rate shows they've seen → the system learns their taste → it predicts how well they'll like this season's new releases, or surfaces classics worth revisiting.

> Full design decisions and roadmap live in [CLAUDE.md](CLAUDE.md) (Chinese). This file covers **current status** and **how to run it**.

---

## Status

**Week 1 · Data layer — done. Week 2 · P0 recommender + API — done.**

| Step | Status |
|---|---|
| Pull Bangumi Archive dump, map field semantics | ✅ |
| Settle candidate-set criteria | ✅ **11,453 titles** |
| Create tables, load data | ✅ `anime_profile` 11,453 rows + `alias` 38,378 rows |
| Backfill staff/studios (from the dump, not AniList) | ✅ 10,576 / 10,688 titles |
| Backfill AniList ids / English titles / popularity | ✅ 6,445 titles (56.3%) |
| Tag cleaning rules + vocabulary | ✅ **308 genre tags** (after a second pass) |
| P0 scoring: tag cosine, mean-centered | ✅ [src/recommend.py](src/recommend.py) |
| Questionnaire selection + sequel folding | ✅ [src/questionnaire.py](src/questionnaire.py) |
| FastAPI endpoints | ✅ [server/main.py](server/main.py) |
| Scoring pushed into Postgres (pgvector) | ✅ [src/recommend_sql.py](src/recommend_sql.py) |
| Deployed to Vercel | ✅ live, all endpoints verified |
| Frontend v0 | ⬜ **next** |

Database: **58 MB / 500 MB** on Neon's free tier.

---

## Stack

| Layer | Choice |
|---|---|
| Data processing | Python 3.12 · polars · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| Database | Neon Postgres 18.4 + pgvector 0.8.1 (`us-east-2`) |
| Chinese tokenization | jieba — Neon can't install `zhparser`, so BM25 requires pre-tokenizing in Python |
| Env management | uv |
| Backend | FastAPI on **Vercel serverless** (see "Why not a warm process" below) |
| Frontend | React + TypeScript + Vite + Tailwind on Vercel |

---

## Getting started

### 1. Environment

```bash
uv sync                       # runtime deps only — what the deployed app needs
uv sync --group etl           # + polars / bgm-tv-wiki / httpx — needed by scripts/
uv sync --group api --group etl --group dev   # everything, for development
```

⚠️ **The main dependency group is deliberately minimal: it is exactly what the
deployed function needs.** Vercel's Python runtime finds `pyproject.toml` +
`uv.lock` and installs *that group* — a `requirements.txt` is ignored entirely.
Anything ETL-only (polars alone drags in a 55 MB runtime) must stay in a group,
or it lands in the function bundle. So `scripts/` need `--group etl`.

`requires-python` is pinned to 3.12 to match the deployment runtime.

### 2. Configuration

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` — DDL and bulk loading. Direct connection, avoiding the conflict between PgBouncer and psycopg3's prepared statements.
- `DATABASE_URL` — the deployed app. Uses the pooler (hostname has an extra `-pooler` segment). Optional locally; the code falls back to the direct URL.
- `CORS_ORIGINS` — comma-separated frontend origins. Defaults to local Vite.

### 3. Fetch the data

```bash
# Latest dump URL comes from aux/latest.json, which includes a sha256
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json

mkdir -p data/raw
curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"    # always verify
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

The archive is ~410 MB compressed, ~1.8 GB extracted.

### 4. Build the database, in this order

```bash
psql < sql/001_init.sql
psql < sql/002_tag_vec.sql                   # don't skip: adds tag_vec / series_root

uv run python scripts/build_id_map.py        # network; downloads bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # network; ~125 requests
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py   # depends on the step above

psql -c 'VACUUM FULL anime_profile'          # reclaim MVCC bloat from the bulk UPDATEs
uv run pytest tests/ -q                      # acceptance: 13 parity tests must pass
```

Every script is idempotent and safe to re-run. **The last two steps are not optional** — skipping `VACUUM FULL` inflates the database to roughly double its real size, and skipping the tests means nobody notices if `tag_vec` was never populated: scoring then returns an empty list *silently*. `GET /health` reports `with_tag_vec` for the same reason.

### 5. Run it

```bash
uv run uvicorn server.main:app --reload      # http://127.0.0.1:8000/docs
uv run pytest tests/ -q                      # after touching either scoring path
uv run ruff check src/ scripts/ server/ tests/
```

> **On Windows:** prefix Python invocations with `PYTHONIOENCODING=utf-8` for scripts that print Chinese to the console. Not needed for the API (it emits JSON) or for `scripts/try_questionnaire.py` (it handles encoding itself).

---

## API

All endpoints are **stateless** — ratings travel with the request, nothing is written server-side. Guests (localStorage) and future registered users (a `user_rating` table) feed the same entry point.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness + `tag_vec` backfill check + tokenizer fingerprint |
| `GET /questionnaire` | Pick questions (`n`, `experience`, `include_nsfw`, `fold_sequels`) |
| `POST /recommend` | Score (`answers`, `mode`, `rank_by`, `min_score`, …) |
| `GET /search` | Search by title/alias — BM25, with a pg_trgm fallback for typos |
| `GET /anime/{id}` | Detail |

Clients send the **answer choice**, not a computed score:

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

The choice → (score, confidence) mapping lives in one place server-side. Letting the client compute it would mean duplicating the mapping into TypeScript, where drift is silent.

---

## Layout

```
src/
  candidates.py      Candidate-set criteria — single source of truth
  tag_rules.py       Tag classification rules + synonym map + import-time self-check
  tagvec.py          The one implementation of the tag vector (log1p × idf × L2)
  recommend.py       In-memory scoring — used by the week-5 offline evaluation
  recommend_sql.py   Postgres scoring — used online; must stay equivalent to the above
  questionnaire.py   Question selection + answer→rating mapping
  textproc.py        jieba tokenization + dictionary fingerprint
server/              FastAPI app (schemas + endpoints)
api/index.py         Vercel entry point — this directory must hold nothing else
sql/                 001 tables · 002 tag_vec + series_root
scripts/             ETL and backfill, one column-set each, never overlapping
tests/test_parity.py Asserts the two scoring paths agree item by item
```

---

## Dataset criteria

```
type == 2                                    # anime
AND has an air year                          # falls back to infobox when date is empty
AND meta_tags ∩ {TV, WEB, Movie, OVA} != ∅   # drops shorts and untagged doujin/MV
AND favorite.done >= 50                      # quality floor
→ 11,453 titles
```

Defined in [src/candidates.py](src/candidates.py). To change the criteria, **edit only that file**.

The `done >= 50` threshold zeroes out three data-quality problems at once — no tags, no score, nobody watched it.

---

## Why not a warm process

The scoring design (an 11,453 × 308 matrix, one matmul per request) wants a long-lived process. Serverless has none: rebuilding the matrix per request costs 2.6 MB of transfer and 1.31 s, against 12 ms of actual scoring. Counter-intuitively, **low traffic makes cold starts worse, not better** — a portfolio project's sparse traffic means most requests hit a cold container.

So the cosine moved into Postgres instead. Per request we now fetch only the works the user rated (~70 ms, almost all round-trip latency) and let pgvector score 11,311 rows — which measures at **≈ 0 ms**, confirming the earlier decision not to build an HNSW index.

The cost is **two scoring implementations**: SQL online, NumPy for the week-5 leave-one-out evaluation (10⁵–10⁶ scorings, far too many for round trips). Two implementations of the same formula is exactly the "two sets of semantics" the design rules forbid, so equivalence is enforced two ways rather than by discipline:

1. `anime_profile.tag_vec` is the **only** definition of the vectors — both paths read the same numbers (verified bit-identical).
2. [tests/test_parity.py](tests/test_parity.py) compares the two paths item by item across every rank mode, time window, and flag combination.

The second one is not redundant. It immediately caught a missing tie-break in the SQL recall pool: because a great many titles are *exactly* orthogonal to a given preference vector, the candidate pool is one large tie — and the two paths were recalling **different candidates** while both looked entirely plausible.

---

## Gotchas (all encountered and verified)

**Chinese BM25 can't use Postgres `tsvector` directly.** Built-in tokenizers treat a whole Chinese sentence as a single token, and Neon can't install `zhparser`/`pgroonga`. Text must be pre-tokenized with jieba before insertion. **Indexing and querying must use the same tokenizer and the same dictionary**, or recall collapses — silently. The app verifies a dictionary fingerprint before serving.

**The `alias` unique constraint must specify `NULLS NOT DISTINCT`.** Either `subject_id` or `character_id` is always NULL, and Postgres treats NULLs as mutually distinct by default — without that clause the constraint is dead for every row.

**An empty `date` doesn't mean "discard".** Of the 213 entries missing only `date`, 97% have the date in their infobox — and they're almost all Chinese animation classics (*Havoc in Heaven*, *Calabash Brothers*, *Black Cat Detective*).

**The biggest tag noise isn't sentiment like "masterpiece" — it's structured data in the wrong place.** Studios and person names outnumber the genre tags themselves. These shouldn't be dropped; they belong in the structured `studios`/`staff` fields.

**Widening the candidate set requires re-auditing the rules.** Extending from 2011+ to all years surfaced 50+ new leaks at once: veteran directors, older IPs, traditional-Chinese variants (`裡番`/`里番`), nostalgia meta-tags. A different era means a different vocabulary.

**A score floor is worth more than a score ceiling.** A high rating doesn't promise a good show, but a low one reliably predicts a bad one. Excluding the 78 titles below 3.5 (0.68% of the library) costs nothing and blocks a specific failure mode: a bad sequel carries almost the same tags as the season you loved, so tag cosine ranks it first — measured at `match=0.983`.

**Anything under `api/` becomes its own Vercel function.** The application package therefore lives in `server/`, with a single entry file in `api/`. Putting `schemas.py` there fails the build.

**Vercel installs your main dependency group, not `requirements.txt`.** Its Python runtime detects `pyproject.toml` + `uv.lock` and runs uv against the *main* group; a hand-maintained `requirements.txt` is silently ignored. The first deploy therefore installed polars and no FastAPI. The fix isn't a deploy setting — it's treating the main group as the deployment manifest and pushing every ETL-only package into a group.

---
---

# animetion_Recommender（中文）

基于偏好问卷的动画推荐系统。用户对看过的番剧评分 → 系统学习口味 → 预测当季新番匹配度，或推荐值得回顾的经典。

> 完整的设计决策与执行计划见 [CLAUDE.md](CLAUDE.md)。本文档只讲**当前状态**和**怎么把它跑起来**。

## 当前进度

**第 1 周数据层完工，第 2 周 P0 推荐 + API 完工。**

| 步骤 | 状态 |
|---|---|
| 拉取 dump、摸清字段语义 | ✅ |
| 确定候选集口径 | ✅ **11,453 部** |
| 建表 + 灌数据 | ✅ `anime_profile` 11,453 行 + `alias` 38,378 行 |
| staff / studios（走 dump 而非 AniList） | ✅ 10,576 / 10,688 部 |
| AniList id / 英文名 / popularity | ✅ 6,445 部（56.3%） |
| Tag 清洗规则与词表 | ✅ **308 个题材 tag**（第二轮清洗后） |
| P0 打分：tag 余弦 + mean-centered | ✅ [src/recommend.py](src/recommend.py) |
| 问卷选题 + 续作折叠 | ✅ [src/questionnaire.py](src/questionnaire.py) |
| FastAPI 接口 | ✅ [server/main.py](server/main.py) |
| 打分推进 Postgres（pgvector） | ✅ [src/recommend_sql.py](src/recommend_sql.py) |
| 部署到 Vercel | ✅ 已上线，四个接口实测通过 |
| 前端 v0 | ⬜ **下一步** |

Neon 免费层占用 **58 MB / 500 MB**。

## 技术栈

| 层 | 选型 |
|---|---|
| 数据处理 | Python 3.12 · polars · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1（`us-east-2`） |
| 中文分词 | jieba —— Neon 装不了 `zhparser`，BM25 只能在 Python 侧预分词 |
| 环境管理 | uv |
| 后端 | FastAPI on **Vercel serverless**（理由见下「为什么不用常驻进程」） |
| 前端 | React + TypeScript + Vite + Tailwind on Vercel |

## 快速开始

### 1. 环境

```bash
uv sync                       # 只装运行时依赖 —— 线上跑的就是这一组
uv sync --group etl           # + polars / bgm-tv-wiki / httpx，scripts/ 要用
uv sync --group api --group etl --group dev   # 开发全量
```

⚠️ **主依赖组是刻意精简的：它等于线上 function 真正需要的东西。**
Vercel 的 Python runtime 检测到 `pyproject.toml` + `uv.lock` 就装**这一组**，
`requirements.txt` 会被完全忽略。任何只在 ETL 用的包（光 polars 就带 55 MB 运行时）
都必须待在 group 里，否则会被打进 function bundle。所以跑 `scripts/` 要加 `--group etl`。

### 2. 配置

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` —— 建表和批量灌数据用。走直连，避开 PgBouncer 与 psycopg3 prepared statement 的冲突
- `DATABASE_URL` —— 线上用，走连接池（主机名多一段 `-pooler`）。本地可不填，代码会退回直连
- `CORS_ORIGINS` —— 前端域名，逗号分隔。不填则默认放行本地 Vite

### 3. 拉数据

```bash
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json
mkdir -p data/raw
curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"    # 务必校验
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

压缩包约 410 MB，解压 1.8 GB。

### 4. 建库，顺序不能乱

```bash
psql < sql/001_init.sql
psql < sql/002_tag_vec.sql                   # 别漏：加 tag_vec / series_root 两列

uv run python scripts/build_id_map.py        # 需要联网，会下 bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # 需要联网，约 125 次请求
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py   # 依赖上一步

psql -c 'VACUUM FULL anime_profile'          # 回收批量 UPDATE 的 MVCC 膨胀
uv run pytest tests/ -q                      # 验收：13 项一致性测试应全绿
```

脚本都幂等，可重复执行。**最后两步不是可选的** —— 跳过 VACUUM 会让库虚涨一倍；跳过 pytest 就没人发现 `tag_vec` 是否漏跑，而它没跑的话打分**静默返回空列表**，不报错。`GET /health` 的 `with_tag_vec` 字段也是为此存在。

### 5. 跑起来

```bash
uv run uvicorn server.main:app --reload      # 接口文档 http://127.0.0.1:8000/docs
uv run pytest tests/ -q                      # 改过任一条打分路径后必跑
uv run ruff check src/ scripts/ server/ tests/
```

> **Windows 注意**：往终端打中文的脚本要加 `PYTHONIOENCODING=utf-8`。API 不需要（它吐 JSON），`scripts/try_questionnaire.py` 也不需要（它自己处理编码）。

## 接口

全部**无状态** —— 评分随请求传入，服务端零写入。游客的 localStorage 和将来注册用户的 `user_rating` 表喂进同一个入口。

| 接口 | 用途 |
|---|---|
| `GET /health` | 存活探针 + `tag_vec` 回填校验 + 分词指纹 |
| `GET /questionnaire` | 选题（`n` / `experience` / `include_nsfw` / `fold_sequels`） |
| `POST /recommend` | 打分（`answers` / `mode` / `rank_by` / `min_score` …） |
| `GET /search` | 按名/别名搜 —— BM25，拼错时退到 pg_trgm 兜底 |
| `GET /anime/{id}` | 详情 |

前端传**作答选项**，不传算好的分数。选项 →（分数, 置信度）的映射只在服务端一处维护 —— 让前端算等于把它复制进 TypeScript，一漂移就是静默的推荐质量下降。

## 目录结构

```
src/
  candidates.py      候选集口径 —— 唯一事实来源
  tag_rules.py       tag 分类规则表 + 同义合并表 + 导入时自检
  tagvec.py          tag 向量的唯一计算实现（log1p × idf × L2）
  recommend.py       内存打分 —— 第 5 周离线评测用
  recommend_sql.py   Postgres 打分 —— 线上用，必须与上面逐条等价
  questionnaire.py   选题 + 作答→评分映射
  textproc.py        jieba 分词 + 词典指纹
server/              FastAPI 应用（schemas + 端点）
api/index.py         Vercel 入口 —— 这个目录**只能放这一个文件**
sql/                 001 建表 · 002 tag_vec + series_root
scripts/             ETL 与回填，各管各的列，绝不交叉
tests/test_parity.py 断言两条打分路径逐条一致
```

## 数据集口径

```
type == 2                                    # 动画
AND 有放送年份                                # date 为空时回退到 infobox
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除短片和无形态标签的同人/MV
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

口径定义在 [src/candidates.py](src/candidates.py)。改口径**只改这一个文件**。

## 为什么不用常驻进程

打分的设计（11,453 × 308 矩阵，每请求一次矩阵乘法）本来需要一个长命进程。serverless 没有：每请求重建矩阵要传 2.6 MB、耗 1.31 s，而打分本身只要 12 ms。反直觉的是，**低流量会让冷启动更糟而不是更好** —— 作品集项目访问零星，大部分请求都会撞上冷容器。

于是把余弦推进了 Postgres。每请求只拉用户评过的那几十部（约 70 ms，几乎全是网络往返），11,311 行的暴力余弦由 pgvector 算 —— 实测**成本 ≈ 0 ms**，印证了此前「不建 HNSW 索引」的判断。

代价是**两套打分实现**：线上 SQL，第 5 周 leave-one-out 评测用 numpy（10⁵–10⁶ 次打分，走往返做不到）。同一个公式两份实现，正是设计原则里禁止的「两套口径」，所以一致性不靠纪律维持，而靠两条构造上的保证：

1. `anime_profile.tag_vec` 是向量的**唯一**定义处，两条路径读同一批数字（实测逐位相同）
2. [tests/test_parity.py](tests/test_parity.py) 在所有排序模式、时间窗口、开关组合下逐条比对输出

第 2 条不是多余的。它立刻抓出了 SQL 召回池少写的一个次级排序键：由于大量作品与给定偏好向量**精确正交**，候选池是一大片并列 —— 两条路径召回的**根本不是同一批候选**，而两边看上去都完全合理。

## 已知的坑（都踩过并验证）

**中文 BM25 不能直接用 Postgres tsvector。** 内置分词器把整句中文当一个 token，而 Neon 装不了 `zhparser`/`pgroonga`。必须在 Python 侧用 jieba 预分词后再入库。**建库与查询必须用同一分词器 + 同一词典**，否则召回直接崩，而且是静默的。应用启动时会校验词典指纹。

**`alias` 的唯一约束必须写 `NULLS NOT DISTINCT`。** 表里 `subject_id` 和 `character_id` 必有一个是 NULL，而 Postgres 默认把 NULL 视为互不相等——不加这句，约束对每一行都失效。

**`date` 为空不等于该丢弃。** 213 部仅缺 `date` 的条目里，97% 的日期就在 infobox 里，而且几乎全是国产经典（大闹天宫、葫芦兄弟、黑猫警长）。

**Tag 里最大的噪声不是「神作」「补番」这类情绪评价，而是结构化信息错位。** 制作公司和人名加起来比题材 tag 还多。这些不该丢弃，而应分流去结构化的 `studios`/`staff` 字段。

**扩大候选集必须重跑规则审查。** 从 2011+ 扩到全年份后，一次性冒出 50+ 个新漏网条目：老一辈监督、老 IP、繁体变体（`裡番`/`里番`）、怀旧元评价。换个年代就是换一套词汇。

**评分下限比评分上限有用。** 高分不保证好看，低分却几乎必然难看。排除 78 部低于 3.5 的作品（占全库 0.68%）几乎没有代价，却挡住了一个具体的失败模式：烂续作的 tag 与你喜欢的那一季几乎相同，于是 tag 余弦把它排到第一 —— 实测 `match=0.983`。

**`api/` 目录下的任何文件都会变成一个独立的 Vercel function。** 所以应用包放在 `server/`，`api/` 只留一个入口文件。把 `schemas.py` 放进去会导致构建失败。

**Vercel 装的是主依赖组，不是 `requirements.txt`。** 它的 Python runtime 检测到 `pyproject.toml` + `uv.lock` 就用 uv 装**主依赖组**，手工维护的 `requirements.txt` 被静默忽略。第一次部署因此装了 polars 却没装 FastAPI。修法不是改部署设置，而是**把主依赖组当作部署清单**，把所有 ETL 专用包挪进 group。

---

## License

See [LICENSE](LICENSE).

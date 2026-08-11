# animetion_Recommender

A preference-questionnaire-driven anime recommender. Users rate shows they've seen → the system learns their taste → it predicts how well they'll like this season's new releases, or surfaces classics worth revisiting.

> Full design decisions and roadmap live in [CLAUDE.md](CLAUDE.md) (Chinese). This file covers **current status** and **how to run it**.

---

## Status

**Week 1 · Data layer** (in progress)

| Step | Status |
|---|---|
| Pull Bangumi Archive dump, map field semantics | ✅ |
| Settle candidate-set criteria | ✅ **11,453 titles** |
| Create tables (`anime_profile` / `alias`) | ✅ |
| Tag cleaning rules + vocabulary | ✅ **418 genre tags** |
| Load data | ⬜ next |
| Backfill staff/studio/English titles from AniList | ⬜ |

---

## Stack

| Layer | Choice |
|---|---|
| Data processing | Python 3.12 · polars · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| Database | Neon Postgres 18.4 + pgvector 0.8.1 (`us-east-2`) |
| Chinese tokenization | jieba — Neon can't install `zhparser`, so BM25 requires pre-tokenizing in Python |
| Env management | uv |
| Backend (week 6) | FastAPI on Render |
| Frontend (week 2) | React + TypeScript + Vite + Tailwind on Vercel |

---

## Getting started

### 1. Environment

```bash
uv sync                  # create .venv, install week-1 deps
uv sync --group embed    # week 3: torch / sentence-transformers / sklearn
uv sync --group api      # week 6: fastapi / uvicorn / argon2 / pyjwt
```

Dependencies are grouped by week — no need to install everything up front. `requires-python` is pinned to 3.12 to match the Render runtime.

### 2. Configuration

```bash
cp .env.example .env
```

Fill in your Neon connection strings. **Both are required:**

- `DATABASE_URL_DIRECT` — for DDL and bulk loading. Uses a direct connection, avoiding the conflict between PgBouncer and psycopg3's prepared statements.
- `DATABASE_URL` — for the production FastAPI app. Uses the pooler (hostname has an extra `-pooler` segment).

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

### 4. Create tables

```bash
uv run python -c "
import os, pathlib, psycopg
from dotenv import load_dotenv; load_dotenv()
with psycopg.connect(os.environ['DATABASE_URL_DIRECT'], autocommit=True) as c:
    c.execute(pathlib.Path('sql/001_init.sql').read_text(encoding='utf-8'))
"
```

The script is idempotent and safe to re-run.

### 5. Build the tag vocabulary

```bash
uv run python scripts/build_tag_vocab.py    # → data/interim/tag_vocab.json
uv run ruff check src/ scripts/             # run after editing tag_rules.py
```

> **On Windows:** prefix Python invocations with `PYTHONIOENCODING=utf-8`, otherwise Chinese output is mojibake under a GBK console.

---

## Layout

```
src/
  candidates.py      Candidate-set criteria — single source of truth.
                     Don't copy filter logic into scripts.
  tag_rules.py       Tag classification rules + synonym map + import-time self-check
sql/
  001_init.sql       anime_profile / alias DDL (idempotent)
scripts/
  explore_subject.py    Dump field-semantics scan (read-only)
  count_candidates.py   Candidate-criteria sizing (read-only, historical snapshot)
  explore_tags.py       Tag distribution analysis (read-only, superseded)
  build_tag_vocab.py    Build the cleaned tag vocabulary
data/                 fully gitignored
  raw/                dump files
  interim/            derived artifacts (tag_vocab.json, …)
```

The `explore_*` scripts are **read-only investigation tools**. They're kept because quarterly dump refreshes require re-verifying that field semantics haven't changed.

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

The `done >= 50` threshold zeroes out three data-quality problems at once — no tags, no score, nobody watched it. Any title 50 people marked as watched has necessarily accumulated tags and ratings.

---

## Gotchas (all encountered and verified)

**Chinese BM25 can't use Postgres `tsvector` directly.** Built-in tokenizers treat a whole Chinese sentence as a single token, and Neon can't install `zhparser`/`pgroonga`. Text must be pre-tokenized with jieba before insertion. **Indexing and querying must use the same tokenizer and the same dictionary**, or recall collapses.

**The `alias` unique constraint must specify `NULLS NOT DISTINCT`.** Either `subject_id` or `character_id` is always NULL, and Postgres treats NULLs as mutually distinct by default — without that clause the constraint is dead for every row.

**An empty `date` doesn't mean "discard".** Of the 213 entries missing only `date`, 97% have the date in their infobox — and they're almost all Chinese animation classics (*Havoc in Heaven*, *Calabash Brothers*, *Black Cat Detective*).

**The biggest tag noise isn't sentiment like "masterpiece" — it's structured data in the wrong place.** 254 studios + 188 person names, more than the genre tags themselves. These shouldn't be dropped; they belong in AniList's structured `studios`/`staff` fields.

**Widening the candidate set requires re-auditing the rules.** Extending from 2011+ to all years surfaced 50+ new leaks at once: veteran directors, older IPs, traditional-Chinese variants (`裡番`/`里番`), nostalgia meta-tags. A different era means a different vocabulary.

**Render's $7 tier didn't disappear.** It moved from being a *plan* to being an *instance type* — Hobby workspace ($0) + Starter instance ($7/mo, no sleep). On the pricing page, the words after "$0/mo **plus compute costs**" are the ones that matter.

---
---

# animetion_Recommender（中文）

基于偏好问卷的动画推荐系统。用户对看过的番剧评分 → 系统学习口味 → 预测当季新番匹配度，或推荐值得回顾的经典。

> 完整的设计决策与执行计划见 [CLAUDE.md](CLAUDE.md)。本文档只讲**当前状态**和**怎么把它跑起来**。

## 当前进度

**第 1 周 · 数据层**（进行中）

| 步骤 | 状态 |
|---|---|
| 拉取 Bangumi Archive dump、摸清字段语义 | ✅ |
| 确定候选集口径 | ✅ **11,453 部** |
| 建表（`anime_profile` / `alias`） | ✅ |
| Tag 清洗规则与词表 | ✅ **418 个题材 tag** |
| 灌数据 | ⬜ 下一步 |
| AniList 补 staff/studio/英文标题 | ⬜ |

## 技术栈

| 层 | 选型 |
|---|---|
| 数据处理 | Python 3.12 · polars · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1（`us-east-2`） |
| 中文分词 | jieba —— Neon 装不了 `zhparser`，BM25 只能在 Python 侧预分词 |
| 环境管理 | uv |
| 后端（第 6 周） | FastAPI on Render |
| 前端（第 2 周） | React + TypeScript + Vite + Tailwind on Vercel |

## 快速开始

### 1. 环境

```bash
uv sync                  # 创建 .venv 并安装第 1 周依赖
uv sync --group embed    # 第 3 周：torch / sentence-transformers / sklearn
uv sync --group api      # 第 6 周：fastapi / uvicorn / argon2 / pyjwt
```

依赖按周分组，不需要一次装全。`requires-python` 锁在 3.12 以对齐 Render 运行时。

### 2. 配置

```bash
cp .env.example .env
```

填入 Neon 连接串。**两条都要填**：

- `DATABASE_URL_DIRECT` —— 建表和批量灌数据用。走直连，避开 PgBouncer 与 psycopg3 prepared statement 的冲突
- `DATABASE_URL` —— 线上 FastAPI 用。走连接池（主机名多一段 `-pooler`）

### 3. 拉数据

```bash
# 最新 dump 地址从 aux/latest.json 取，含 sha256
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json

mkdir -p data/raw
curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"    # 务必校验
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

压缩包约 410 MB，解压 1.8 GB。

### 4. 建表

```bash
uv run python -c "
import os, pathlib, psycopg
from dotenv import load_dotenv; load_dotenv()
with psycopg.connect(os.environ['DATABASE_URL_DIRECT'], autocommit=True) as c:
    c.execute(pathlib.Path('sql/001_init.sql').read_text(encoding='utf-8'))
"
```

脚本幂等，可重复执行。

### 5. 构建 tag 词表

```bash
uv run python scripts/build_tag_vocab.py    # → data/interim/tag_vocab.json
uv run ruff check src/ scripts/             # 改过 tag_rules.py 后跑一次
```

> **Windows 用户注意**：所有 Python 调用请加 `PYTHONIOENCODING=utf-8`，否则中文输出在 GBK 终端下会乱码。

## 目录结构

```
src/
  candidates.py      候选集口径 —— 唯一事实来源，脚本不要各自复制筛选逻辑
  tag_rules.py       tag 分类规则表 + 同义合并表 + 导入时自检
sql/
  001_init.sql       anime_profile / alias 建表（幂等）
scripts/
  explore_subject.py    dump 字段语义扫描（只读）
  count_candidates.py   候选集口径试算（只读，历史快照）
  explore_tags.py       tag 分布分析（只读，已被取代）
  build_tag_vocab.py    构建清洗后的 tag 词表
data/                 全部 gitignore
  raw/                dump 原始文件
  interim/            中间产物（tag_vocab.json 等）
```

三个 `explore_*` 是**只读探查工具**，保留是因为季度同步时 dump 更新需要重跑一遍验证字段语义没变。

## 数据集口径

```
type == 2                                    # 动画
AND 有放送年份                                # date 为空时回退到 infobox
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除短片和无形态标签的同人/MV
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

口径定义在 [src/candidates.py](src/candidates.py)。改口径**只改这一个文件**。

`done >= 50` 这个阈值同时把三个数据质量问题清零——无 tag、无评分、无人看过全部归零。有 50 人标记看过的条目必然已积累 tag 和评分。

## 已知的坑（都踩过并验证）

**中文 BM25 不能直接用 Postgres tsvector。** 内置分词器把整句中文当一个 token，而 Neon 装不了 `zhparser`/`pgroonga`。必须在 Python 侧用 jieba 预分词后再入库。**建库与查询必须用同一分词器 + 同一词典**，否则召回直接崩。

**`alias` 的唯一约束必须写 `NULLS NOT DISTINCT`。** 表里 `subject_id` 和 `character_id` 必有一个是 NULL，而 Postgres 默认把 NULL 视为互不相等——不加这句，约束对每一行都失效。

**`date` 为空不等于该丢弃。** 213 部仅缺 `date` 的条目里，97% 的日期就在 infobox 里，而且几乎全是国产经典（大闹天宫、葫芦兄弟、黑猫警长）。

**Tag 里最大的噪声不是「神作」「补番」这类情绪评价，而是结构化信息错位。** 制作公司 254 个 + 人名 188 个，比题材 tag 还多。这些不该丢弃，而应分流去 AniList 的结构化 `studios`/`staff` 字段。

**扩大候选集必须重跑规则审查。** 从 2011+ 扩到全年份后，一次性冒出 50+ 个新漏网条目：老一辈监督、老 IP、繁体变体（`裡番`/`里番`）、怀旧元评价。换个年代就是换一套词汇。

**Render 的 $7 没消失。** 它从「计划」变成了「实例类型」——Hobby 工作区（$0）+ Starter 实例（$7/月，不休眠）。定价页上「$0/mo plus compute costs」的 plus 后面才是重点。

---

## License

See [LICENSE](LICENSE).

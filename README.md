# animetion_Recommender

A preference-questionnaire-driven anime recommender, plus a grounded Q&A layer
over a Chinese-language plot corpus. Rate what you've seen → the system learns
your taste → it predicts how much you'll like this season's releases. Separately,
ask about plots, voice actors or airing seasons, or describe a show you're trying
to find.

Live at `animetion-recommender.vercel.app` — frontend and API in one Vercel
project, same origin.

> Design decisions, measurements and open questions live in [CLAUDE.md](CLAUDE.md) (Chinese).
> This file is **status + how to run it**.

---

## What it does

| | |
|---|---|
| **Recommend** | Questionnaire → preference vector → scoring over the whole library. Three signals fused: 308 genre tags, summary embeddings, staff/studio. **Zero model calls on the request path.** |
| **Ask** | One input box, four branches — plot Q&A, voice-actor filmography, airing season, semantic show-finding. Routed by rules, then validated by one LLM call. |
| **Accounts** | Username (no email — there is no mail capability), argon2 + JWT in an httpOnly cookie, rating sync, 10 questions per 24h. |

```
11,453 titles          candidate set, single source of truth — src/candidates.py
132,056 chunks         plot corpus, covers 79.3% of titles / 97.3% popularity-weighted
263,690 aliases        subject + character + person
145,306 voice roles    8,215 voice actors
19,381 spoiler flags   offline gating, from Moegirl's own heimu markup
1,001 MB               Neon Postgres, paid plan
329 tests              uv run --group etl python -m pytest tests/ -q
```

**The result the project was built to produce:** when the retrieved material
actually contains the answer, the model answers correctly **93.8%** of the time —
but the material contains the answer only **50.0%** of the time. The bottleneck is
retrieval and corpus coverage, not generation. Two rounds of corpus work moved
that second number from 30.8% to 50.0%. Full report:
[docs/week5-eval-report.md](docs/week5-eval-report.md).

It also settled the premise the project was pitched on. 142 titles had an
all-zero tag vector — mostly Western and older Chinese animation, whose official
genre tags are far sparser — so tag cosine could **never** retrieve them.
Embeddings recovered **131 of them (92%)**. The nearest neighbours of *Havoc in
Heaven* went from modern web-novel adaptations (its only tags being `玄幻` +
`小说改`) to *The Golden Monkey Defeats the Demon*, *Journey to the West* and
*Ginseng Fruit* — all Shanghai Animation Film Studio.

---

## What's next

Six items, none blocking the others.

| Item | Where it stands |
|---|---|
| **Quarterly update** | Refreshing existing titles is nearly free — the pipeline is idempotent, skips unchanged rows by md5, and already stores Moegirl's `lastrevid`. The blocker is admission: `favorite.done >= 50` keeps new shows out by construction. Measured: latest `air_date` is 2026-09-11, only 2 titles air in the future, and all 6 titles with `air_year >= 2027` have **no `air_date` at all** — so an "airs within N months" rule has to handle the NULL. Decide the admission rule (it changes the candidate set) and the tag/staff vocabulary policy first. |
| **Information-gain question selection** | Questions are picked for diversity (MMR) today. Next is picking the question whose answer most reduces uncertainty, so ten answers buy more than they do now. |
| **Detail page** | `GET /api/anime/{id}` has served since week 2; no frontend page calls it. |
| **English support** | Season routing already handles English. Three gaps: voice triggers are Chinese-only; `MENTION_MAX = 16` < `fullmetalalchemist` (18); and **14 of 33 common English words collide with real aliases** (`protagonist`, `comedy`, `school`), so descriptive English resolves to some title and never reaches show-finding. The corpus is Chinese, so measure the cross-language penalty **first** — the 150-question auto-annotated eval set translated to English is a clean A/B and nearly free. |
| **Episode synopses** | The dump carries **108,835 episode descriptions** across 10,630 titles, none of it loaded. Would answer "what happens in episode 12", which currently fails outright. |
| **Voice actors as a recommendation feature** | `staff_vec` has directors, music and studios but **zero voice-actor credits** — they need a two-hop join through `subject-characters` + `person-characters`. The data exists (`voice_role`). Adding it changes the vocabulary size, so `sql/006`'s column width and `staffvec.DIM` move together and the library gets recomputed. |

| Week | Content | Status |
|---|---|---|
| 1–3 | Data layer · P0 scoring · questionnaire · deploy · embeddings · P1 fusion | ✅ |
| 4 | Moegirl corpus · character corpus · corpus → Chinese · retrieval pipeline | ✅ |
| 5 | **Offline evaluation** | ✅ |
| 5.5 | Parser fixes · character pages · voice-actor casting | ✅ |
| 6 | Accounts ✅ · frontend ✅ · information-gain selection ⬜ · quarterly sync ⬜ | 🔄 |

---

## Stack

| Layer | Choice |
|---|---|
| Data | Python 3.12 · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| Database | Neon Postgres 18.4 + pgvector 0.8.1 (`us-east-2`) |
| Tokenization | jieba — Neon can't install `zhparser`, so BM25 needs pre-tokenizing in Python |
| Embeddings | Qwen3-Embedding-0.6B · `halfvec(1024)` — **locked, never swapped** |
| Reranking | `BAAI/bge-reranker-v2-m3` — swappable; its output is a relative ordering |
| Generation | Qwen3-14B (answers) · Qwen3-8B (intent, voice-actor phrasing) |
| Auth | argon2id + PyJWT in an httpOnly cookie, same origin |
| Backend | FastAPI on Vercel serverless · Frontend: React + TS + Vite + Tailwind |

⚠️ **The embedding model is the one component that can never fall back to another
provider.** Its output is a coordinate relative to the vectors already stored;
mixing two encoders yields a plausible-looking, correctly sorted list of noise,
with no error raised. The LLM and the reranker have no such constraint — their
outputs are text and orderings, used once and discarded. If embeddings became
unavailable, the correct degradation is BM25, not a different encoder.

---

## Getting started

### 1. Install

```bash
uv sync                       # runtime deps only — exactly what the deployed function needs
uv sync --group etl           # + bgm-tv-wiki / tqdm / lxml — needed by scripts/
uv sync --group api --group etl --group dev   # everything, for development
```

⚠️ **The main dependency group is the deployment manifest.** Vercel's Python
runtime finds `pyproject.toml` + `uv.lock` and installs *that group* — a
`requirements.txt` is ignored entirely. Anything on the request path (`httpx`,
`argon2-cffi`, `pyjwt`) must live there or production dies at module-level import
and takes the whole ASGI app down; anything ETL-only must stay in a group or it
lands in the function bundle.

### 2. Configure

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` — DDL and bulk loading. Direct connection, avoiding the PgBouncer / psycopg3 prepared-statement conflict.
- `DATABASE_URL` — the deployed app; uses the pooler. Optional locally.
- `SILICONFLOW_API_KEY` — embeddings, reranking and generation share one key.
- `AUTH_SECRET` — session signing, ≥32 chars. **The app refuses to start without it rather than falling back to a default**, which would let a misconfigured deploy issue normal-looking tokens signed with a publicly known key.
- `CORS_ORIGINS` — **local development only.** Production is same-origin, so the middleware never runs.

⚠️ **Deploying needs exactly three: `DATABASE_URL`, `SILICONFLOW_API_KEY`,
`AUTH_SECRET`.** Do not set `DATABASE_URL_DIRECT` in production — the pool
resolves `DATABASE_URL or DATABASE_URL_DIRECT`, so a typo in the first silently
falls back to a direct connection: the service starts, looks fine, and quietly
exhausts Neon's connection limit. Each has its own check — `/api/health` returning
`catalog_size` proves the database, registering proves `AUTH_SECRET`, asking a
plot question proves the API key.

### 3. Fetch the dump

```bash
# Latest URL comes from aux/latest.json, which includes a sha256
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json
mkdir -p data/raw && curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"    # always verify
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

~410 MB compressed, ~1.8 GB extracted.

### 4. Build the database, in this order

```bash
uv sync --group etl
for f in sql/0*.sql; do psql < "$f"; done   # 001 tables … 007 corpus, 009 voice, 010 accounts

uv run python scripts/build_id_map.py        # network; downloads bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # network; ~125 requests
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py
uv run python scripts/build_embeddings.py    # needs SILICONFLOW_API_KEY; ~12 min / ¥0.19
uv run python scripts/build_staff_vectors.py
uv run --group ml python scripts/build_clusters.py       # mmr_rank + cluster_id
uv run --group etl python scripts/build_voice_roles.py   # ~3 min, no API calls

# ── corpus: the one large wall-clock cost ───────────────────────
uv run --group etl python scripts/fetch_moegirl.py       # ~4 h (7 s/request — do not lower)
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/build_char_chunks.py   # dump character bios, no fetching
uv run --group etl python scripts/extract_char_links.py --sizes
uv run --group etl python scripts/fetch_char_pages.py --top 1000   # ~10 h, resumable
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

psql -c 'VACUUM FULL anime_profile'          # reclaim MVCC bloat from the bulk UPDATEs
uv run --group etl python -m pytest tests/ -q     # acceptance: 329 tests
```

Every script is idempotent. **The last two steps are not optional** — skipping
`VACUUM FULL` roughly doubles the apparent database size, and skipping the tests
means nobody notices if `tag_vec` was never populated: scoring then returns an
empty list *silently*. `GET /health` reports `with_tag_vec` for the same reason.

⚠️ **Run tests with `--group etl`.** 28 of them call `pytest.importorskip("lxml")`
and are **skipped rather than failed** without it — a green run can mean they
never executed. Check the summary line for `skipped`.

⚠️ **Two caches, very different replacement costs.** `data/interim/embed_cache/`
(~50 MB, not in git) costs ¥0.19 and twelve minutes to rebuild — but copying it
from an old machine makes the rebuild free and bit-identical, which is the only
way two machines can build the same library when the encoder is a remote API.
`data/interim/translate_cache/` (50 MB) is the **only** copy of 43,932
translations; losing it means re-translating for about eight hours. Back that one
up separately.

### 5. Run

```bash
uv run uvicorn server.main:app --reload      # API, docs at /api/docs
cd web && npm install && npm run dev         # frontend :5173, /api proxied to :8000
uv run --group etl python -m pytest tests/ -q
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint
```

> **On Windows:** prefix with `PYTHONIOENCODING=utf-8` for scripts that print Chinese.

---

## API

Everything lives under `/api`. **The recommendation path is stateless** — ratings
travel with the request; guests (localStorage) and signed-in users feed the same
entry point, and the account layer sits alongside that path, not inside it.

| Endpoint | Purpose | Model calls |
|---|---|---|
| `GET /health` | Liveness + five backfill-coverage fields + tokenizer fingerprint | — |
| `GET /questionnaire` | Pick questions | — |
| `POST /recommend` | Score | — |
| `GET /search` | Title/alias search — BM25, pg_trgm fallback for typos | — |
| `GET /anime/{id}` | Detail | — |
| `GET /season` | Browse a broadcast season | — |
| `GET /related` | Other works by the same author/director/studio | — |
| `GET /voice` | What roles a voice actor has played | — |
| `GET /find` | Semantic show-finding from a description | 1 embedding |
| `POST /ask` | **Single entry point** — plot Q&A / voice / season / find | 2+ |

Accounts: `POST /auth/register|login|logout`, `GET /auth/me`,
`PUT /auth/username|password`, `GET|PUT|DELETE /ratings`, `GET /ratings/detail`.

⚠️ **The line between "open to guests" and "needs an account" is whether the
endpoint spends money, not whether it looks like Q&A.** `/voice` and `/season` are
pure SQL and stay open; `/find` makes one embedding call and still requires an
account, because leaving it open is a free side door around the quota.

Clients send the **answer choice**, not a computed score:

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

The choice → (score, confidence) mapping lives in one place server-side; letting
the client compute it would duplicate it into TypeScript, where drift is silent.
The same rule is why the database stores the *choice* rather than the derived
score.

---

## Layout

```
src/
  candidates.py    Candidate-set criteria — single source of truth
  tag_rules.py     Tag classification + synonym map + import-time self-check
  tagvec.py        The one implementation of the tag vector (log1p × idf × L2)
  recommend.py     In-memory scoring — offline evaluation
  recommend_sql.py Postgres scoring — online; must stay equivalent to the above
  questionnaire.py Question selection + answer→rating mapping
  textproc.py      jieba tokenization + dictionary fingerprint
  embed.py         The only definition of the embedding model — locked, fingerprinted
  retrieve.py      Retrieval pipeline + entity-resolution state machine. Read-only
  rerank.py        Reranker client — swappable, unlike embed.py
  llm.py           Generation, intent classification, prompts, config fingerprint
  router.py        Intent dispatch — pure functions, no model, no database
  find.py / voice.py / related.py     Show-finding · casting · related works
  auth.py / ratings.py / quota.py     Accounts · rating sync · Q&A quota
server/            FastAPI app — every route under /api
web/               Vite + React + Tailwind (same Vercel project)
  session.tsx      The single entry point for session + ratings — see below
api/index.py       Vercel entry point — this directory must hold nothing else
sql/ scripts/ tests/
```

🚨 **`web/src/session.tsx` is the file to read first on the frontend.** Components
above it don't know whether the user is signed in — they receive
`{answers, setAnswer}`, and that module decides whether a rating lands in
localStorage or syncs to the account. It's the frontend counterpart of the
server-side rule that ratings travel with the request. The moment a component
writes its own `if (user) … else …`, the rule breaks in several places at once,
each drifting separately.

---

## Dataset criteria

```
type == 2                                    # anime
AND has an air year                          # falls back to infobox when date is empty
AND meta_tags ∩ {TV, WEB, Movie, OVA} != ∅   # drops shorts and untagged doujin/MV
AND favorite.done >= 50                      # quality floor
→ 11,453 titles
```

Defined in [src/candidates.py](src/candidates.py) — **to change the criteria, edit
only that file.** `done >= 50` zeroes out three data-quality problems at once (no
tags, no score, nobody watched it). It is also why new shows can't enter the
library, which is the open decision behind the quarterly update above.

---

## Why not a warm process

Scoring (an 11,453 × 308 matrix, one matmul per request) wants a long-lived
process. Serverless has none: rebuilding the matrix per request costs 2.6 MB of
transfer and 1.31 s, against 12 ms of actual scoring. Counter-intuitively, **low
traffic makes cold starts worse** — sparse traffic means most requests hit a cold
container. So the cosine moved into Postgres: per request we fetch only the works
the user rated (~70 ms, almost all round-trip) and let pgvector score 11,311 rows,
which measures at **≈ 0 ms**.

The cost is **two scoring implementations** — SQL online, NumPy for the offline
leave-one-out evaluation. Equivalence is enforced by construction, not discipline:
`anime_profile.tag_vec` is the only definition of the vectors (both paths read the
same numbers, verified bit-identical), and
[tests/test_parity.py](tests/test_parity.py) compares the two paths item by item
across every mode and flag combination. That second check immediately caught a
missing tie-break in the SQL recall pool: a great many titles are *exactly*
orthogonal to a given preference vector, so the pool is one large tie — the two
paths were recalling **different candidates** while both looked entirely
plausible.

---

## Gotchas

**Chinese BM25 can't use Postgres `tsvector` directly.** Built-in tokenizers treat a whole Chinese sentence as one token, and Neon can't install `zhparser`. Text is pre-tokenized with jieba; indexing and querying must use the same tokenizer *and* the same dictionary or recall collapses silently, so the app verifies a dictionary fingerprint before serving.

**The `alias` unique constraint needs `NULLS NOT DISTINCT`.** Either `subject_id` or `character_id` is always NULL, and Postgres treats NULLs as distinct by default — without it the constraint is dead for every row. It recurred later: voice-actor rows have *both* ids NULL, so `person_id` had to join the constraint or same-named actors would silently drop.

**A serverless pool needs `check=ConnectionPool.check_connection`.** Neon reclaims idle connections and psycopg's pool can't tell; the next request gets a dead socket and 500s. `pg_terminate_backend` does **not** reproduce it — that sends a RST, visible at once — you have to let a connection idle out for real.

**"The answer isn't in the material" often means the list of material is incomplete.** A title's own Bangumi summary — the most authoritative answer to "what is this about" — was never in the retrieval pool. Separately, the evaluation sheet rendered only retrieved chunks, so injected material was invisible to the grader, who scored sourced answers as hallucinations. Ask whether the list is complete before asking why the answer isn't in it.

**Metrics drift out from under their own definition.** The evaluation scored "answered without a retrieval hit" as hallucination. A later prompt change deliberately turned bare refusals into grounded partial answers — the hallucination rate went 22.2% → 84.6% while, reading all 26 cases, **not one was fabricated**. Nothing about the number itself looked wrong.

**Rules beat tuned thresholds when the score is noisy.** Reranker scores carry ~1e-3 of run-to-run noise and opening/ending-theme chunks legitimately score 0.003–0.028, so no absolute floor separates them from junk. Reserving a seat for the best song chunk fixed 5 of 7 failing questions at +0.09 chunks of context; lowering the floor fixed 4 and cost +3.44. **38× cheaper, and it doesn't drift.**

**Widening the candidate set requires re-auditing the rules.** Extending from 2011+ to all years surfaced 50+ new leaks at once — veteran directors, older IPs, traditional-Chinese variants (`裡番`/`里番`), nostalgia meta-tags. A different era means a different vocabulary.

**Anything under `api/` becomes its own Vercel function.** The application package lives in `server/`, with a single entry file in `api/`. Putting `schemas.py` there fails the build.

---
---

# animetion_Recommender（中文）

基于偏好问卷的动画推荐系统，外加一层建立在中文剧情语料之上的问答。
给看过的番打分 → 系统学习口味 → 预测当季新番匹配度；
另一条线是问剧情、问声优、问档期，或者描述一下想找的番。

已上线：`animetion-recommender.vercel.app` —— 前端与 API 同一个 Vercel 项目、同源。

> 设计决策、实测数据与未决问题见 [CLAUDE.md](CLAUDE.md)。本文档只讲**现状**和**怎么跑起来**。

---

## 它能做什么

| | |
|---|---|
| **推荐** | 问卷 → 偏好向量 → 全库打分。三路信号融合：308 个题材 tag、简介 embedding、staff/studio。**请求路径上零模型调用。** |
| **问答** | 一个输入框，四条分支 —— 剧情问答、声优配役、档期浏览、语义找番。先按规则分派，再过一次 LLM 意图校验。 |
| **账号** | 用户名（不收邮箱 —— 本站没有发信能力），argon2 + httpOnly cookie 里的 JWT，评分同步，24 小时 10 条问答配额。 |

```
11,453 部     候选集，唯一事实来源 —— src/candidates.py
132,056 条    剧情语料，覆盖作品 79.3% / 热度加权 97.3%
263,690 行    别名：作品 + 角色 + 人物
145,306 条    声优配役，涉及 8,215 位声优
19,381 条     剧透标注，离线门控，来自萌娘自己的 heimu 标记
1,001 MB      Neon Postgres，付费计划
329 项测试     uv run --group etl python -m pytest tests/ -q
```

**这个项目要产出的核心结论：资料里确实有答案时，模型答对率 93.8%；
而资料里有答案的只有 50.0%。** 瓶颈在检索与语料覆盖，不在生成。
两轮语料改动把后一个数字从 30.8% 提到了 50.0%。
完整报告见 [docs/week5-eval-report.md](docs/week5-eval-report.md)。

它同时验证了立项时的假设。有 142 部作品的 tag 向量**全为零** ——
以欧美动画和国产老动画为主，官方题材标签对非日本作品明显更稀疏 ——
它们在 tag 余弦下**永远无法被召回**。embedding 救回了其中 **131 部（92%）**。
《大闹天宫》的最近邻从《修罗武神》《长生界》这类现代网文改（它的 tag 只有
`玄幻` + `小说改`），变成了《金猴降妖》《西游记》《人参果》—— 全是上美影的西游题材。

---

## 接下来要做的

六件事，互不阻塞。

| 事项 | 卡在哪 |
|---|---|
| **季度更新** | 更新已有作品几乎免费 —— 管道幂等、md5 跳过未变行、萌娘 `lastrevid` 一直在存。阻塞在准入：`favorite.done >= 50` 从构造上把新番挡在门外。实测：最晚 `air_date` 是 2026-09-11、未来只有 2 部、`air_year >= 2027` 的 6 部**全都没有 `air_date`** —— 所以「未来 N 个月内开播」这条规则必须处理 NULL。先定准入规则（它会改变候选集）和 tag/staff 词表策略。 |
| **信息增益选题** | 现在按多样性挑（MMR）。下一步是挑「答案最能降低口味不确定性」的那道题，让十道题问出更多信息。 |
| **详情页** | `GET /api/anime/{id}` 从第 2 周就在服务，前端没有任何页面调它。 |
| **英文支持** | 档期路由已认英文。三个缺口：声优触发词全中文；`MENTION_MAX = 16` 比 `fullmetalalchemist`（18）短；**33 个常见英文词里 14 个撞上真实别名**（`protagonist`、`comedy`、`school`），于是描述性英文句子会假命中某部作品、永远够不着找番兜底。语料是中文的，所以**跨语言惩罚要先量再定方案** —— 150 题自动标注评测集翻成英文重跑就是干净的 A/B，几乎零成本。 |
| **单集简介** | dump 里有 **108,835 条分集简介**（涉及 10,630 部），一条都没灌。它能回答「第 12 话讲了什么」，目前完全答不了。 |
| **声优岗位进推荐** | `staff_vec` 有导演、音乐、制作公司，但**没有任何声优字段** —— 声优要走 `subject-characters` + `person-characters` 两跳。数据已经有了（`voice_role`）。加进去会改变词表规模，`sql/006` 列宽和 `staffvec.DIM` 要一起改、全库重算。 |

| 周 | 内容 | 状态 |
|---|---|---|
| 1–3 | 数据层 · P0 打分 · 问卷 · 部署 · embedding · P1 融合 | ✅ |
| 4 | 萌娘语料 · 角色语料 · 语料转中文 · 检索层 | ✅ |
| 5 | **离线评测** | ✅ |
| 5.5 | 解析器修复 · 角色页 · 声优配役 | ✅ |
| 6 | 账号 ✅ · 前端 ✅ · 信息增益选题 ⬜ · 季度同步 ⬜ | 🔄 |

---

## 技术栈

| 层 | 选型 |
|---|---|
| 数据处理 | Python 3.12 · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1（`us-east-2`） |
| 分词 | jieba —— Neon 装不了 `zhparser`，BM25 只能在 Python 侧预分词 |
| Embedding | Qwen3-Embedding-0.6B · `halfvec(1024)` —— **锁死，绝不更换** |
| 重排 | `BAAI/bge-reranker-v2-m3` —— 可以换，输出是相对排序 |
| 生成 | Qwen3-14B（回答）· Qwen3-8B（意图分类、声优文案） |
| 认证 | argon2id + PyJWT，放 httpOnly cookie，同源 |
| 部署 | FastAPI on Vercel serverless · 前端 React + TS + Vite + Tailwind |

⚠️ **embedding 是全项目唯一不能 fallback 到另一个厂商的组件。**
它的输出是**相对于库里那批向量的坐标**；混用两个编码器会得到一个看起来完全正常、
排序也像模像样的噪声列表，而且不报任何错。LLM 和 rerank 没有这个约束 ——
它们的输出是文本和相对排序，用完即弃。真的断供了，正确的降级方向是**退回 BM25**。

---

## 快速开始

### 1. 装依赖

```bash
uv sync                       # 只装运行时依赖 —— 线上 function 需要的就这些
uv sync --group etl           # + bgm-tv-wiki / tqdm / lxml，scripts/ 要用
uv sync --group api --group etl --group dev   # 开发全量
```

⚠️ **主依赖组就是部署清单。** Vercel 的 Python runtime 检测到 `pyproject.toml` +
`uv.lock` 就装**这一组**，`requirements.txt` 会被完全忽略。凡是在请求路径上的包
（`httpx`、`argon2-cffi`、`pyjwt`）都必须在里面，否则线上会**在模块级 import 就炸**、
把整个 ASGI app 一起带走；而只在 ETL 用的包必须待在 group 里，
否则会被打进 function bundle。

### 2. 配置

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` —— 建表和批量灌数据。走直连，避开 PgBouncer 与 psycopg3 prepared statement 的冲突
- `DATABASE_URL` —— 线上用，走连接池。本地可不填
- `SILICONFLOW_API_KEY` —— embedding / rerank / 生成**共用这一个 key**
- `AUTH_SECRET` —— 会话签名，≥32 字符。**缺失时应用直接启动失败、绝不退回默认值** —— 有默认值的话，忘配的部署会正常签发 token，而密钥是公开在源码里的
- `CORS_ORIGINS` —— **只在本地开发有用**。线上同源，中间件根本不参与

⚠️ **部署只需要三个：`DATABASE_URL`、`SILICONFLOW_API_KEY`、`AUTH_SECRET`。**
不要把 `DATABASE_URL_DIRECT` 配到线上 —— 连接池写的是
`DATABASE_URL or DATABASE_URL_DIRECT`，前者哪天拼错会**静默退回直连**：
服务照常起来、功能看着正常，只是在悄悄耗尽 Neon 的连接数。
三个变量各有独立验证路径：`/api/health` 回 `catalog_size` → 库通；
注册账号 → `AUTH_SECRET` 通；问一条剧情问题 → API key 通。

### 3. 拉数据

```bash
curl -sL https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json
mkdir -p data/raw && curl -L -o data/raw/dump.zip "<browser_download_url>"
sha256sum -c <<< "<digest>  data/raw/dump.zip"    # 务必校验
python -c "import zipfile; zipfile.ZipFile('data/raw/dump.zip').extractall('data/raw/dump')"
```

压缩包约 410 MB，解压 1.8 GB。

### 4. 建库，顺序不能乱

```bash
uv sync --group etl
for f in sql/0*.sql; do psql < "$f"; done   # 001 建表 … 007 语料、009 声优、010 账号

uv run python scripts/build_id_map.py        # 需要联网，会下 bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # 需要联网，约 125 次请求
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py
uv run python scripts/build_embeddings.py    # 需要 SILICONFLOW_API_KEY；约 12 分钟 / ¥0.19
uv run python scripts/build_staff_vectors.py
uv run --group ml python scripts/build_clusters.py       # mmr_rank + cluster_id
uv run --group etl python scripts/build_voice_roles.py   # 约 3 分钟，无 API 调用

# ── 语料：唯一一笔大墙钟开销 ──────────────────────────────────
uv run --group etl python scripts/fetch_moegirl.py       # 约 4 小时（7 秒/请求，别调低）
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/build_char_chunks.py   # dump 角色简介，零抓取
uv run --group etl python scripts/extract_char_links.py --sizes
uv run --group etl python scripts/fetch_char_pages.py --top 1000   # 约 10 小时，可断点续跑
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

psql -c 'VACUUM FULL anime_profile'          # 回收批量 UPDATE 的 MVCC 膨胀
uv run --group etl python -m pytest tests/ -q     # 验收：329 项
```

脚本都幂等。**最后两步不是可选的** —— 跳过 VACUUM 会让库虚涨一倍；
跳过 pytest 就没人发现 `tag_vec` 是否漏跑，而它没跑的话打分**静默返回空列表**，
不报错。`GET /health` 的 `with_tag_vec` 也是为此存在。

⚠️ **跑测试要带 `--group etl`。** 其中 28 项调了 `pytest.importorskip("lxml")`，
没有那一组时它们是**被跳过而不是失败** —— 「全绿」可能只是没跑。看有没有 `skipped`。

⚠️ **两个缓存，重建代价完全不同。** `data/interim/embed_cache/`（约 50 MB，不入 git）
重建要 ¥0.19 和 12 分钟 —— 但从旧机器拷过去就是零成本且 bit-identical，
这是编码器为远程 API 时唯一能保证两台机器建出同一个库的办法。
`data/interim/translate_cache/`（50 MB）则是 43,932 条译文的**唯一副本**，
丢了是重翻 8 小时。这一份要单独备份。

### 5. 跑起来

```bash
uv run uvicorn server.main:app --reload      # API，文档在 /api/docs
cd web && npm install && npm run dev         # 前端 :5173，/api 代理到 :8000
uv run --group etl python -m pytest tests/ -q
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint
```

> **Windows 注意**：往终端打中文的脚本要加 `PYTHONIOENCODING=utf-8`。

---

## 接口

全部在 `/api` 下。**推荐这条链路是无状态的** —— 评分随请求传入，
游客的 localStorage 和登录用户喂进同一个入口；账号层**并排**在它旁边，不在它里面。

| 接口 | 用途 | 模型调用 |
|---|---|---|
| `GET /health` | 存活探针 + 五个回填覆盖率字段 + 分词指纹 | — |
| `GET /questionnaire` | 选题 | — |
| `POST /recommend` | 打分 | — |
| `GET /search` | 按名/别名搜 —— BM25，拼错时退 pg_trgm | — |
| `GET /anime/{id}` | 详情 | — |
| `GET /season` | 按档期浏览 | — |
| `GET /related` | 同作者 / 导演 / 公司的其他作品 | — |
| `GET /voice` | 某声优配过哪些角色 | — |
| `GET /find` | 按描述语义找番 | 1 次 embedding |
| `POST /ask` | **单一入口** —— 剧情问答 / 声优 / 档期 / 找番 | 2+ |

账号：`POST /auth/register|login|logout`、`GET /auth/me`、
`PUT /auth/username|password`、`GET|PUT|DELETE /ratings`、`GET /ratings/detail`。

⚠️ **「游客能用」与「要登录」的判据是这个端点会不会花钱，不是它像不像问答。**
`/voice`、`/season` 是纯 SQL，对游客开放；`/find` 只调一次 embedding 却仍要登录，
因为不拦的话它就是**绕过配额的后门**。

前端传**作答选项**，不传算好的分数：

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

选项 →（分数, 置信度）的映射只在服务端一处维护 —— 让前端算等于把它复制进
TypeScript，一漂移就是静默的推荐质量下降。同一条纪律也是**库里存 choice
而不存算好的分数**的原因。

---

## 目录结构

```
src/
  candidates.py    候选集口径 —— 唯一事实来源
  tag_rules.py     tag 分类规则 + 同义合并 + 导入时自检
  tagvec.py        tag 向量的唯一计算实现（log1p × idf × L2）
  recommend.py     内存打分 —— 离线评测用
  recommend_sql.py Postgres 打分 —— 线上用，必须与上面逐条等价
  questionnaire.py 选题 + 作答→评分映射
  textproc.py      jieba 分词 + 词典指纹
  embed.py         embedding 模型的唯一定义处 —— 锁死、带指纹
  retrieve.py      检索管道 + 实体解析状态机。只读库
  rerank.py        重排客户端 —— 可以换模型，与 embed.py 不同
  llm.py           生成、意图分类、prompt、配置指纹
  router.py        意图分派 —— 纯函数，零模型、不碰库
  find.py / voice.py / related.py     找番 · 配役 · 关联查询
  auth.py / ratings.py / quota.py     账号 · 评分同步 · 问答配额
server/            FastAPI 应用 —— 所有路由都在 /api 下
web/               Vite + React + Tailwind（同一个 Vercel 项目）
  session.tsx      会话与评分的**单一入口**，见下
api/index.py       Vercel 入口 —— 这个目录**只能放这一个文件**
sql/ scripts/ tests/
```

🚨 **`web/src/session.tsx` 是前端最该先读的文件。** 上层组件**不知道用户登没登录** ——
它们拿到的永远是 `{answers, setAnswer}`，数据存 localStorage 还是同步到账号由它决定。
这是服务端那条「评分随请求传入」铁律在前端的对应物。一旦让某个组件自己写
`if (user) … else …`，这条铁律就会在多处同时破掉、各自漂移。

---

## 数据集口径

```
type == 2                                    # 动画
AND 有放送年份                                # date 为空时回退到 infobox
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除短片和无形态标签的同人/MV
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

口径定义在 [src/candidates.py](src/candidates.py)，**改口径只改这一个文件**。
`done >= 50` 一次清零了三个数据质量问题 —— 无 tag、无评分、无人看过。
它同时也是新番进不来的原因，正是上面「季度更新」那条待决策的点。

---

## 为什么不用常驻进程

打分（11,453 × 308 矩阵，每请求一次矩阵乘法）本来需要一个长命进程。
serverless 没有：每请求重建矩阵要传 2.6 MB、耗 1.31 s，而打分本身只要 12 ms。
反直觉的是，**低流量会让冷启动更糟** —— 访问零星意味着大部分请求都撞上冷容器。
于是把余弦推进了 Postgres：每请求只拉用户评过的那几十部（约 70 ms，几乎全是往返），
11,311 行的暴力余弦由 pgvector 算，实测**成本 ≈ 0 ms**。

代价是**两套打分实现** —— 线上 SQL，离线 leave-one-out 评测用 numpy。
一致性靠构造保证而非纪律：`anime_profile.tag_vec` 是向量的唯一定义处
（两条路径读同一批数字，实测逐位相同），
[tests/test_parity.py](tests/test_parity.py) 在所有模式与开关组合下逐条比对。
第二条立刻抓出了 SQL 召回池少写的一个次级排序键：由于大量作品与偏好向量
**精确正交**，候选池是一大片并列 —— 两条路径召回的**根本不是同一批候选**，
而两边看上去都完全合理。

---

## 已知的坑

**中文 BM25 不能直接用 Postgres tsvector。** 内置分词器把整句中文当一个 token，而 Neon 装不了 `zhparser`。必须用 jieba 预分词后入库；**建库与查询必须用同一分词器 + 同一词典**，否则召回直接崩且是静默的，所以应用启动时会校验词典指纹。

**`alias` 的唯一约束必须写 `NULLS NOT DISTINCT`。** `subject_id` 和 `character_id` 必有一个是 NULL，而 Postgres 默认把 NULL 视为互不相等 —— 不加这句约束对每一行都失效。后来换个位置复发了一次：声优行**两个 id 都是 NULL**，所以 `person_id` 必须也加进约束，否则同名声优会被静默丢掉。

**serverless 的连接池必须配 `check=ConnectionPool.check_connection`。** Neon 会回收空闲连接而 psycopg 的池不知道，下一个请求拿到死连接就 500。⚠️ `pg_terminate_backend` **复现不出**（它发 RST，socket 层立刻可见），必须真晾满空闲窗口。

**「资料里没有」往往只是你看不见它。** 作品自己的 Bangumi 简介 —— 问「讲了什么故事」时最权威的那份文本 —— **从来不在检索池里**。而打分表只渲染检索到的 chunk，别处注入的资料对打分的人不可见，于是有出处的回答被判成了幻觉。**问「答案为什么不在资料里」之前，先问「资料清单是不是完整的」。**

**指标会从自己的定义下面漂走。** 评测把「未命中却给了答案」判为幻觉。后来一次 prompt 改动**有意**把干巴巴的拒答变成有据的部分回答 —— 幻觉率从 22.2% 涨到 84.6%，而把那 26 道逐条读完，**没有一道是编造的**。数字本身看起来完全正常。

**分数有噪声时，规则完胜调阈值。** rerank 分的批间噪声约 1e-3，而 OP/ED 类 chunk 本身就只有 0.003–0.028，任何绝对地板都分不开它和垃圾。给最佳歌曲 chunk **占一个席位**，7 道失败题修好 5 道，代价是每题上下文 +0.09 条；把地板降到 0 只修好 4 道却要 +3.44 条。**便宜 38 倍，而且不会漂。**

**扩大候选集必须重跑规则审查。** 从 2011+ 扩到全年份后一次性冒出 50+ 个新漏网条目：老一辈监督、老 IP、繁体变体（`裡番`/`里番`）、怀旧元评价。换个年代就是换一套词汇。

**`api/` 下的任何文件都会变成一个独立的 Vercel function。** 所以应用包放在 `server/`，`api/` 只留一个入口文件。把 `schemas.py` 放进去会导致构建失败。

---

## License

See [LICENSE](LICENSE).

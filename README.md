# animetion_Recommender

A preference-questionnaire-driven anime recommender, plus a grounded Q&A layer
over a Chinese-language plot corpus. Users rate shows they've seen → the system
learns their taste → it predicts how well they'll like this season's new
releases. Separately, they can ask about plots, voice actors, or airing seasons,
or describe a show they are trying to find.

> Full design decisions and roadmap live in [CLAUDE.md](CLAUDE.md) (Chinese). This file covers **current status** and **how to run it**.

---

## Status

**Weeks 1–6 are implemented.** Deployed at `animetion-recommender.vercel.app` —
frontend and API in one Vercel project, same origin.

**Recommendation (weeks 1–3)**

| Step | Status |
|---|---|
| Pull Bangumi Archive dump, map field semantics | ✅ |
| Settle candidate-set criteria | ✅ **11,453 titles** |
| Create tables, load data | ✅ `anime_profile` 11,453 rows |
| Backfill staff/studios (from the dump, not AniList) | ✅ 10,576 / 10,688 titles |
| Backfill AniList ids / English titles / popularity | ✅ 6,445 titles (56.3%) |
| Tag cleaning rules + vocabulary | ✅ **308 genre tags** (after a second pass) |
| P0 scoring: tag cosine, mean-centered | ✅ [src/recommend.py](src/recommend.py) |
| Questionnaire selection + sequel folding | ✅ [src/questionnaire.py](src/questionnaire.py) |
| Scoring pushed into Postgres (pgvector) | ✅ [src/recommend_sql.py](src/recommend_sql.py) |
| Qwen3 embeddings over every summary | ✅ **10,864 / 11,453** vectors · ¥0.19 · 11m44s |
| Local embedding cache (reproducibility) | ✅ [src/embed_cache.py](src/embed_cache.py) — the API is not per-request deterministic |
| Questionnaire diversity (MMR, not k-means) | ✅ redundancy 0.4552 → 0.3781 |
| P1: fusing tag + embedding + staff/studio | ✅ [src/staffvec.py](src/staffvec.py) · `sparsevec(1933)` in 0.47 MB |

**Corpus and retrieval (weeks 4–5.5)**

| Step | Status |
|---|---|
| Moegirl series pages | ✅ 2,301 pages |
| Moegirl character pages | ✅ **7,362 pages → 42,183 chunks** |
| Bangumi character bios | ✅ 69,417 chunks |
| Whole corpus normalised to Chinese | ✅ Japanese residue **0.36%** (titles) / **0.81%** (characters) · 43,932 cached translations |
| `plot_chunk` + scope mapping | ✅ **132,056 chunks** · covers **79.3%** of titles, **97.3%** popularity-weighted |
| Offline spoiler gating | ✅ 19,381 chunks flagged, from Moegirl's own `heimu` markup |
| Retrieval pipeline (alias pin → vector recall → rerank → floor) | ✅ [src/retrieve.py](src/retrieve.py) |
| Reranking (`bge-reranker-v2-m3`) | ✅ [src/rerank.py](src/rerank.py) |
| **Offline evaluation** | ✅ [docs/week5-eval-report.md](docs/week5-eval-report.md) |

**Q&A features**

| Step | Status |
|---|---|
| Plot Q&A, grounded, with source links | ✅ `POST /api/ask` |
| Voice-actor filmography (weighted ranking + LLM phrasing) | ✅ `person` 8,215 · `voice_role` **145,306** |
| Airing-season browse | ✅ `GET /api/season` — also recognises English time expressions |
| Semantic show-finding | ✅ `GET /api/find` + an LLM gate that declines vague input |
| Single entry point with intent routing | ✅ [src/router.py](src/router.py) — four branches, forceable via `route` |
| LLM intent validation on every request | ✅ `llm.classify_intent()` |
| Structured related-works lookup | ✅ `GET /api/related` — zero model calls |

**Accounts and frontend (week 6)**

| Step | Status |
|---|---|
| Accounts (username, not email — there is no mail capability) | ✅ [src/auth.py](src/auth.py) · argon2 + JWT in an httpOnly cookie |
| Rating persistence + guest merge | ✅ [src/ratings.py](src/ratings.py) |
| Q&A quota (10 per 24h, reserve-then-refund) | ✅ [src/quota.py](src/quota.py) |
| Full frontend | ✅ home · questionnaire · rate-by-search · results · Q&A · account page |

Database **1,001 MB** on a paid Neon plan · `alias` **263,690 rows** ·
**329 tests** (`uv run --group etl python -m pytest tests/ -q`).

---

## What's next

Six items, none of them blocking the others. Ordered by how much is already in
place, not by importance.

### 1. Quarterly update

The pipeline is already idempotent and skips unchanged rows by md5, and
`fetch_moegirl.py` already stores Moegirl's `lastrevid` — so refreshing
*existing* titles is close to free. The blocker is admission: `favorite.done >= 50`
keeps brand-new shows out by construction.

Measured: the latest `air_date` in the library is **2026-09-11**, only **2**
titles air in the future, and all **6** titles with `air_year >= 2027` have **no
`air_date` at all** — so an "airs within N months" rule has to handle the NULL or
it will miss exactly the batch it was written for.

Two decisions come before any code: the admission rule (it changes the candidate
set, which is the single source of truth for every evaluation) and whether the
tag/staff vocabularies freeze per quarter or get rebuilt annually. A frozen
vocabulary silently ignores new tags; a growing one changes vector dimensions and
forces a full recompute.

### 2. Information-gain question selection

Questions are currently picked for diversity (MMR). The next step is choosing the
question whose answer is expected to reduce uncertainty about the user's taste the
most — so that ten answers buy more than ten answers currently do.

### 3. Detail page

`GET /api/anime/{id}` has been serving since week 2; there is simply no page in
the frontend that calls it. The last unbuilt item from the week-2 backlog.

### 4. English support

Partly there, and measured rather than guessed. Season routing already understands
English (`ten years ago`, `summer of 2016`). Of 8 English probes, `resolve()`
correctly identified 4 titles and characters.

Three real gaps:

- Voice-actor triggers are Chinese-only, so "Who voices Mikasa?" falls through to plot Q&A.
- `MENTION_MAX = 16` is shorter than `fullmetalalchemist` (18 characters).
- **14 of 33 common English words collide with real aliases** — `protagonist`, `comedy`, `school`, `key` are all legitimate character or title names. So a descriptive English sentence falsely resolves to *some* title, lands in `AMBIGUOUS`, and never reaches the show-finding fallback, which only fires on `UNKNOWN`.

The corpus is Chinese, so the cross-language retrieval penalty has to be measured
**before** choosing an approach — a Chinese query against a Japanese corpus was
once measured at −27.7 percentage points, and this is the same shape of problem.
The 150-question auto-annotated evaluation set translated into English is a clean
A/B and costs almost nothing. Do that first; it decides whether inbound
translation is needed at all.

### 5. Episode synopses

The dump carries **108,835 episode descriptions** (median 193 characters, across
10,630 titles) and none of it is loaded — there is no episode table yet. It would
answer "what happens in episode 12", which currently fails outright, and it is an
authoritative source, unlike Moegirl's episode tables which carry titles only.

### 6. Voice actors as a recommendation feature

`staff_vec` covers directors, music and studios, but **zero titles carry a
voice-actor credit** — voice actors are not in `subject-persons` and need a
two-hop join through `subject-characters` + `person-characters`. The data already
exists (`voice_role`, 145,306 rows, built for the Q&A feature). Adding it changes
the vocabulary size, so `sql/006`'s column width and `staffvec.DIM` move together
and the whole library gets recomputed — which is why it is not a drive-by change.

---

## Roadmap

| Week | Content | Status |
|---|---|---|
| 1 | Data layer: dump → candidate set → load → tag cleaning | ✅ |
| 2 | P0 scoring, questionnaire, sequel folding, API, pgvector, frontend v0, deploy | ✅ |
| 3 | Qwen3 embeddings · P1 fusing staff/studio · questionnaire diversity | ✅ |
| 4 | Moegirl corpus · character corpus · corpus → Chinese · LLM selection · retrieval | ✅ |
| 5 | **Offline evaluation — the point of the project** | ✅ |
| 5.5 | Parser fixes · character pages · voice-actor casting | ✅ |
| 6 | Accounts ✅ · frontend ✅ · information-gain selection ⬜ · quarterly sync ⬜ | 🔄 |

Week 5 is what makes this a portfolio piece rather than a demo. The headline
result is a separation the project was built to expose: **when the retrieved
material actually contains the answer, the model answers correctly 93.8% of the
time — but the material contains the answer only 50.0% of the time.** The
bottleneck is retrieval and corpus coverage, not generation. Two rounds of corpus
and retrieval work moved that second number from 30.8% to 50.0%.

Week 3 settled the premise the project was pitched on. 142 titles had an
all-zero tag vector — mostly Western animation and older Chinese productions,
whose official genre tags are far sparser — so tag cosine could **never** retrieve
them. Embeddings recovered **131 of them (92%)**, leaving 11. Concretely, the
nearest neighbours of *Havoc in Heaven* went from modern web-novel adaptations
(its only tags being `玄幻` + `小说改`) to *The Golden Monkey Defeats the Demon*,
*Journey to the West*, and *Ginseng Fruit* — all Shanghai Animation Film Studio.
That before/after pair is a reproducible case, not a lone NDCG number.

⚠️ **Chunk loading should go in batches with a plain `VACUUM`, not `VACUUM FULL`.**
It rewrites the whole table, and on Neon that WAL is billed as instant-restore
storage. Since the paid upgrade this is a cost note rather than a hard cliff —
going over no longer suspends the project. The real cost variable is compute
($0.105/CU-hour, capped at 0.5 CU), not storage.

---

## Stack

| Layer | Choice |
|---|---|
| Data processing | Python 3.12 · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| Database | Neon Postgres 18.4 + pgvector 0.8.1 (`us-east-2`) |
| Chinese tokenization | jieba — Neon can't install `zhparser`, so BM25 requires pre-tokenizing in Python |
| Embeddings | Qwen3-Embedding-0.6B via SiliconFlow · `halfvec(1024)` — **locked, never swapped** |
| Reranking | `BAAI/bge-reranker-v2-m3` — swappable, its output is a relative ordering |
| Generation | Qwen3-14B (answers) · Qwen3-8B (intent classification, voice-actor phrasing) |
| Auth | argon2id + PyJWT in an httpOnly cookie, same origin |
| Env management | uv |
| Backend | FastAPI on **Vercel serverless** (see "Why not a warm process" below) |
| Frontend | React + TypeScript + Vite + Tailwind on Vercel |

⚠️ **The embedding model is the one component that can never fall back to another
provider.** Its output is a coordinate relative to the vectors already in the
database; mixing two encoders produces a plausible-looking, correctly sorted list
of noise, with no error raised. The LLM and the reranker carry no such constraint
— their outputs are text and relative orderings, used once and discarded. If
embeddings became unavailable, the correct degradation is falling back to BM25,
not to a different encoder.

---

## Getting started

### 1. Environment

```bash
uv sync                       # runtime deps only — what the deployed app needs
uv sync --group etl           # + bgm-tv-wiki / tqdm / lxml — needed by scripts/
uv sync --group api --group etl --group dev   # everything, for development
```

⚠️ **The main dependency group is deliberately minimal: it is exactly what the
deployed function needs.** Vercel's Python runtime finds `pyproject.toml` +
`uv.lock` and installs *that group* — a `requirements.txt` is ignored entirely.
Anything ETL-only must stay in a group, or it lands in the function bundle —
so `scripts/` need `--group etl`. (The audit that followed also found polars
was never imported anywhere, so it is gone entirely.)

`requires-python` is pinned to 3.12 to match the deployment runtime.

### 2. Configuration

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` — DDL and bulk loading. Direct connection, avoiding the conflict between PgBouncer and psycopg3's prepared statements.
- `DATABASE_URL` — the deployed app. Uses the pooler (hostname has an extra `-pooler` segment). Optional locally; the code falls back to the direct URL.
- `SILICONFLOW_API_KEY` — embeddings, reranking and generation all share one key.
- `AUTH_SECRET` — session signing. **At least 32 characters; the app refuses to start without it rather than falling back to a default**, because a default would let a misconfigured deployment issue perfectly normal-looking tokens signed with a publicly known key.
- `CORS_ORIGINS` — **local development only.** Defaults to Vite's 5173. Do not set it in production: the frontend and the API share one origin there, so the CORS middleware never runs. A cross-origin error in production means the client is calling some other host — check `web/src/api.ts`, not this variable.

⚠️ **Deploying needs exactly three of these: `DATABASE_URL`, `SILICONFLOW_API_KEY`,
`AUTH_SECRET`.** Do **not** set `DATABASE_URL_DIRECT` in production — nothing on
the request path reads it, and because the pool resolves `DATABASE_URL or
DATABASE_URL_DIRECT`, having both means a typo in the first silently falls back to
a direct connection: the service starts, everything looks fine, and it quietly
exhausts Neon's connection limit. Each of the three has its own check:
`/api/health` returning `catalog_size` proves the database, registering an account
proves `AUTH_SECRET`, and asking a plot question proves the API key.

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
uv sync --group etl                          # scripts/ need this group (lxml, tqdm, ...)
psql < sql/001_init.sql
psql < sql/002_tag_vec.sql                   # don't skip: adds tag_vec / series_root
psql < sql/003_vec_halfvec.sql               # vec: vector(1024) → halfvec(1024). Idempotent
psql < sql/004_build_meta.sql                # don't skip: build_embeddings.py preflights on it
psql < sql/005_mmr_rank.sql                  # diversity ordering for questionnaire items
psql < sql/006_staff_vec.sql                 # staff/studio vector column for P1
psql < sql/007_plot_chunk.sql                # corpus: three tables
psql < sql/008_translation.sql               # translation backup table
psql < sql/009_voice_role.sql                # person / voice_role
psql < sql/010_auth.sql                      # app_user / user_rating / ask_log

uv run python scripts/build_id_map.py        # network; downloads bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # network; ~125 requests
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py   # depends on the step above
uv run python scripts/build_embeddings.py    # needs SILICONFLOW_API_KEY in .env
                                             # ~12 min / ¥0.19 (instant on a cache hit)
uv run python scripts/build_staff_vectors.py # staff_vec + data/interim/staff_vocab.json
uv run --group ml python scripts/build_clusters.py       # mmr_rank + cluster_id; needs vec
uv run --group etl python scripts/build_voice_roles.py   # ~3 min, no API calls

# ── corpus: the one large wall-clock cost ───────────────────────
uv run --group etl python scripts/fetch_moegirl.py       # ~4 h (7 s/request — do not lower)
uv run --group etl python scripts/parse_moegirl.py
uv run --group etl python scripts/build_plot_chunks.py
uv run --group etl python scripts/build_char_chunks.py   # dump character bios; no fetching
uv run --group etl python scripts/extract_char_links.py --sizes
uv run --group etl python scripts/fetch_char_pages.py --top 1000   # ~10 h, resumable
uv run --group etl python scripts/parse_moegirl.py --kind character
uv run --group etl python scripts/build_plot_chunks.py --kind character

psql -c 'VACUUM FULL anime_profile'          # reclaim MVCC bloat from the bulk UPDATEs
uv run --group etl python -m pytest tests/ -q     # acceptance: all 329 tests must pass
```

Every script is idempotent and safe to re-run. **The last two steps are not optional** — skipping `VACUUM FULL` inflates the database to roughly double its real size, and skipping the tests means nobody notices if `tag_vec` was never populated: scoring then returns an empty list *silently*. `GET /health` reports `with_tag_vec` for the same reason.

⚠️ **Run the tests with `--group etl`.** 28 of them call `pytest.importorskip("lxml")`, and without that group they are **skipped rather than failed** — so a green run can simply mean they never executed. Check the summary line for `skipped`.

`SILICONFLOW_API_KEY` is the one thing a fresh machine needs a human to go and get (see `.env.example`); `build_embeddings.py` fails fast without it, before spending anything. The embedding cache under `data/interim/embed_cache/` is not in git (~50 MB), so a new machine pays the ¥0.19 again — **but copying that directory over from an old machine makes the rebuild free and bit-identical**, which is the only way to get two machines to build the same library when the encoder is a remote API.

⚠️ **`data/interim/translate_cache/` (50 MB) is the only copy of 43,932 translations.** Losing it means re-translating for about eight hours; losing the embedding cache costs ¥0.19 and twelve minutes. Different orders of magnitude — back the translation cache up separately.

### 5. Run it

```bash
uv run uvicorn server.main:app --reload      # API, docs at /api/docs
cd web && npm install && npm run dev         # frontend at :5173, /api proxied to :8000
uv run --group etl python -m pytest tests/ -q     # after touching either scoring path
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint   # frontend checks
```

> **On Windows:** prefix Python invocations with `PYTHONIOENCODING=utf-8` for scripts that print Chinese to the console. Not needed for the API (it emits JSON) or for `scripts/try_questionnaire.py` (it handles encoding itself).

---

## API

Every route lives under `/api`.

**The recommendation path is stateless.** Ratings travel with the request and
nothing is written server-side. Guests (localStorage) and signed-in users feed the
same entry point; the account layer sits alongside that path, not inside it.

| Endpoint | Purpose | Model calls |
|---|---|---|
| `GET /health` | Liveness + five backfill-coverage fields + tokenizer fingerprint | — |
| `GET /questionnaire` | Pick questions (`n`, `experience`, `include_nsfw`, `fold_sequels`) | — |
| `POST /recommend` | Score (`answers`, `mode`, `rank_by`, `min_score`, …) | — |
| `GET /search` | Search by title/alias — BM25, with a pg_trgm fallback for typos | — |
| `GET /anime/{id}` | Detail | — |
| `GET /season` | Browse a broadcast season | — |
| `GET /related` | Other works by the same author/director/studio | — |
| `GET /voice` | What roles a voice actor has played | — |
| `GET /find` | Semantic show-finding from a description | 1 embedding |
| `POST /ask` | **Single entry point** — routes to plot Q&A / voice / season / find | 2+ |

**Accounts:** `POST /auth/register`, `/auth/login`, `/auth/logout`,
`GET /auth/me`, `PUT /auth/username`, `PUT /auth/password`,
`GET|PUT|DELETE /ratings`, `GET /ratings/detail`.

⚠️ **The line between "open to guests" and "needs an account" is whether the
endpoint spends money, not whether it looks like Q&A.** `/voice` and `/season` are
pure SQL and stay open even though they answer questions; `/find` makes only one
embedding call but still requires an account, because leaving it open would be a
free side door around the quota — the same work at a different URL.

Clients send the **answer choice**, not a computed score:

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

The choice → (score, confidence) mapping lives in one place server-side. Letting
the client compute it would mean duplicating the mapping into TypeScript, where
drift is silent. The same rule is why the database stores the *choice* rather than
the derived score: baking today's mapping into rows would mean that changing it
later requires rewriting the table, and that old rows silently carry the old
semantics in the meantime.

---

## Layout

```
src/
  candidates.py      Candidate-set criteria — single source of truth
  tag_rules.py       Tag classification rules + synonym map + import-time self-check
  tagvec.py          The one implementation of the tag vector (log1p × idf × L2)
  recommend.py       In-memory scoring — used by the offline evaluation
  recommend_sql.py   Postgres scoring — used online; must stay equivalent to the above
  questionnaire.py   Question selection + answer→rating mapping
  textproc.py        jieba tokenization + dictionary fingerprint
  embed.py           The only definition of the embedding model — locked, fingerprinted
  retrieve.py        Retrieval pipeline + entity-resolution state machine. Read-only
  rerank.py          Reranker client — swappable, unlike embed.py
  llm.py             Generation, intent classification, prompts, config fingerprint
  router.py          Intent dispatch — pure functions, no model, no database
  find.py            Semantic show-finding
  voice.py           Voice-actor casting lookup
  related.py         Structured related-works lookup
  auth.py            Password hashing, session tokens, username normalisation
  ratings.py         Rating persistence + guest merge
  quota.py           Q&A quota — reserve first, refund on server error
server/              FastAPI app — every route is under /api
web/                 Vite + React + Tailwind frontend (same Vercel project)
  session.tsx        The single entry point for session + ratings — see below
api/index.py         Vercel entry point — this directory must hold nothing else
sql/                 001 tables · … · 007 corpus · 009 voice roles · 010 accounts
scripts/             ETL and backfill, one column-set each, never overlapping
tests/test_parity.py Asserts the two scoring paths agree item by item
```

🚨 **`web/src/session.tsx` is the file to read first on the frontend.** Components
above it do not know whether the user is signed in — they receive
`{answers, setAnswer}` and nothing else, and that module decides whether a rating
lands in localStorage or syncs to the account. It is the frontend counterpart of
the server-side rule that ratings travel with the request. The moment a component
writes its own `if (user) … else …`, the rule is broken in several places at once,
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

Defined in [src/candidates.py](src/candidates.py). To change the criteria, **edit only that file**.

The `done >= 50` threshold zeroes out three data-quality problems at once — no tags, no score, nobody watched it. It is also the reason brand-new shows cannot enter the library, which is the open decision behind the quarterly update above.

---

## Why not a warm process

The scoring design (an 11,453 × 308 matrix, one matmul per request) wants a long-lived process. Serverless has none: rebuilding the matrix per request costs 2.6 MB of transfer and 1.31 s, against 12 ms of actual scoring. Counter-intuitively, **low traffic makes cold starts worse, not better** — a portfolio project's sparse traffic means most requests hit a cold container.

So the cosine moved into Postgres instead. Per request we now fetch only the works the user rated (~70 ms, almost all round-trip latency) and let pgvector score 11,311 rows — which measures at **≈ 0 ms**, confirming the earlier decision not to build an HNSW index.

The cost is **two scoring implementations**: SQL online, NumPy for the offline leave-one-out evaluation (10⁵–10⁶ scorings, far too many for round trips). Two implementations of the same formula is exactly the "two sets of semantics" the design rules forbid, so equivalence is enforced two ways rather than by discipline:

1. `anime_profile.tag_vec` is the **only** definition of the vectors — both paths read the same numbers (verified bit-identical).
2. [tests/test_parity.py](tests/test_parity.py) compares the two paths item by item across every rank mode, time window, and flag combination.

The second one is not redundant. It immediately caught a missing tie-break in the SQL recall pool: because a great many titles are *exactly* orthogonal to a given preference vector, the candidate pool is one large tie — and the two paths were recalling **different candidates** while both looked entirely plausible.

---

## Gotchas (all encountered and verified)

**Chinese BM25 can't use Postgres `tsvector` directly.** Built-in tokenizers treat a whole Chinese sentence as a single token, and Neon can't install `zhparser`/`pgroonga`. Text must be pre-tokenized with jieba before insertion. **Indexing and querying must use the same tokenizer and the same dictionary**, or recall collapses — silently. The app verifies a dictionary fingerprint before serving.

**The `alias` unique constraint must specify `NULLS NOT DISTINCT`.** Either `subject_id` or `character_id` is always NULL, and Postgres treats NULLs as mutually distinct by default — without that clause the constraint is dead for every row. The same trap recurred later: voice-actor rows have *both* ids NULL, so `person_id` had to join the constraint, or two same-named actors would collide and one would be dropped in silence.

**A serverless connection pool needs `check=ConnectionPool.check_connection`.** Neon reclaims idle connections and psycopg's pool cannot tell; the next request gets a dead socket and 500s. It looks rare in production only because traffic is near zero and most requests land on fresh containers — the actual trigger is "container still alive, connection already reclaimed". Note that `pg_terminate_backend` does **not** reproduce it (that sends a RST, visible immediately at the socket layer); you have to let a connection idle out for real.

**An empty `date` doesn't mean "discard".** Of the 213 entries missing only `date`, 97% have the date in their infobox — and they're almost all Chinese animation classics (*Havoc in Heaven*, *Calabash Brothers*, *Black Cat Detective*).

**The biggest tag noise isn't sentiment like "masterpiece" — it's structured data in the wrong place.** Studios and person names outnumber the genre tags themselves. These shouldn't be dropped; they belong in the structured `studios`/`staff` fields.

**Widening the candidate set requires re-auditing the rules.** Extending from 2011+ to all years surfaced 50+ new leaks at once: veteran directors, older IPs, traditional-Chinese variants (`裡番`/`里番`), nostalgia meta-tags. A different era means a different vocabulary.

**A score floor is worth more than a score ceiling.** A high rating doesn't promise a good show, but a low one reliably predicts a bad one. Excluding the 78 titles below 3.5 (0.68% of the library) costs nothing and blocks a specific failure mode: a bad sequel carries almost the same tags as the season you loved, so tag cosine ranks it first — measured at `match=0.983`.

**"The answer isn't in the material" often means the list of material is incomplete.** Two instances, same shape. A title's own Bangumi summary — the most authoritative answer to "what is this about" — was never in the retrieval pool at all. And the evaluation sheet rendered only the retrieved chunks, so material injected from other sources was invisible to the person grading: they saw "not in the material", saw the model answer anyway, and scored it as a hallucination when it was in fact sourced. **Before asking why the answer isn't in the material, ask whether the list of material is complete.**

**Metrics can drift out from under their own definition.** The evaluation scored "answered without a retrieval hit" as a hallucination and "declined" as a non-answer. A later prompt change deliberately turned bare refusals into grounded partial answers — so the hallucination rate went from 22.2% to 84.6% while, on reading all 26 cases, **not one was fabricated**. The instrument had stopped measuring what its name claimed, and nothing about the number itself looked wrong.

**Rules beat tuned thresholds when the score is noisy.** Reranker scores carry ~1e-3 of run-to-run noise, and opening/ending-theme chunks legitimately score 0.003–0.028 — so no absolute floor separates them from junk. Reserving a seat for the best song chunk fixed 5 of 7 failing questions at a cost of +0.09 chunks of context per question; lowering the floor instead fixed only 4 and cost +3.44. **38× cheaper, and it doesn't drift.**

**Anything under `api/` becomes its own Vercel function.** The application package therefore lives in `server/`, with a single entry file in `api/`. Putting `schemas.py` there fails the build.

**Vercel installs your main dependency group, not `requirements.txt`.** Its Python runtime detects `pyproject.toml` + `uv.lock` and runs uv against the *main* group; a hand-maintained `requirements.txt` is silently ignored. The first deploy therefore installed polars and no FastAPI. The fix isn't a deploy setting — it's treating the main group as the deployment manifest and pushing every ETL-only package into a group. Anything on the request path — `httpx`, `argon2-cffi`, `pyjwt` — has to live in the main group, or production dies at module-level import and takes the whole ASGI app down with it.

---
---

# animetion_Recommender（中文）

基于偏好问卷的动画推荐系统，外加一层建立在中文剧情语料之上的问答。
用户对看过的番剧评分 → 系统学习口味 → 预测当季新番匹配度；
另一条线是问剧情、问声优、问档期，或者描述一下想找的番。

> 完整的设计决策与执行计划见 [CLAUDE.md](CLAUDE.md)。本文档只讲**当前状态**和**怎么把它跑起来**。

## 当前进度

**第 1–6 周全部实现完毕。** 部署在 `animetion-recommender.vercel.app` ——
前端与 API 同一个 Vercel 项目、同源。

**推荐（第 1–3 周）**

| 步骤 | 状态 |
|---|---|
| 拉取 dump、摸清字段语义 | ✅ |
| 确定候选集口径 | ✅ **11,453 部** |
| 建表 + 灌数据 | ✅ `anime_profile` 11,453 行 |
| staff / studios（走 dump 而非 AniList） | ✅ 10,576 / 10,688 部 |
| AniList id / 英文名 / popularity | ✅ 6,445 部（56.3%） |
| Tag 清洗规则与词表 | ✅ **308 个题材 tag**（第二轮清洗后） |
| P0 打分：tag 余弦 + mean-centered | ✅ [src/recommend.py](src/recommend.py) |
| 问卷选题 + 续作折叠 | ✅ [src/questionnaire.py](src/questionnaire.py) |
| 打分推进 Postgres（pgvector） | ✅ [src/recommend_sql.py](src/recommend_sql.py) |
| Qwen3 embedding 全库编码 | ✅ **10,864 / 11,453** 条 · ¥0.19 · 11 分 44 秒 |
| Embedding 本地缓存（可复现性） | ✅ [src/embed_cache.py](src/embed_cache.py) —— API 并非逐请求确定 |
| 问卷选题多样化（MMR，非 k-means） | ✅ 冗余 0.4552 → 0.3781 |
| P1：tag + embedding + staff/studio 三路融合 | ✅ [src/staffvec.py](src/staffvec.py) · `sparsevec(1933)` 仅占 0.47 MB |

**语料与检索（第 4–5.5 周）**

| 步骤 | 状态 |
|---|---|
| 萌娘百科作品页 | ✅ 2,301 页 |
| 萌娘百科角色页 | ✅ **7,362 页 → 42,183 chunk** |
| Bangumi 角色简介 | ✅ 69,417 chunk |
| 全部语料统一为中文 | ✅ 日文残留 **0.36%**（作品）/ **0.81%**（角色）· 43,932 条译文缓存 |
| `plot_chunk` + 作用域映射 | ✅ **132,056 条** · 覆盖作品 **79.3%**、热度加权 **97.3%** |
| 离线剧透门控 | ✅ 19,381 条标注，来自萌娘自己的 `heimu` 标记 |
| 检索管道（alias 直取 → 向量召回 → rerank → 地板） | ✅ [src/retrieve.py](src/retrieve.py) |
| 重排（`bge-reranker-v2-m3`） | ✅ [src/rerank.py](src/rerank.py) |
| **离线评测** | ✅ [docs/week5-eval-report.md](docs/week5-eval-report.md) |

**问答功能**

| 步骤 | 状态 |
|---|---|
| 剧情问答，全部有出处 | ✅ `POST /api/ask` |
| 声优配役（加权排序 + LLM 组织语言） | ✅ `person` 8,215 · `voice_role` **145,306** |
| 按档期浏览 | ✅ `GET /api/season` —— 同时认英文时间表达 |
| 语义找番 | ✅ `GET /api/find` + 一道 LLM 门控，描述太模糊就请用户说具体 |
| 单一入口 + 意图路由 | ✅ [src/router.py](src/router.py) —— 四条分支，可用 `route` 强制指定 |
| 每条请求都过一次意图校验 | ✅ `llm.classify_intent()` |
| 结构化关联查询 | ✅ `GET /api/related` —— 零模型调用 |

**账号与前端（第 6 周）**

| 步骤 | 状态 |
|---|---|
| 账号系统（用户名而非邮箱 —— 本站没有发信能力） | ✅ [src/auth.py](src/auth.py) · argon2 + httpOnly cookie 里的 JWT |
| 评分持久化 + 游客数据合并 | ✅ [src/ratings.py](src/ratings.py) |
| 问答配额（24 小时 10 条，先扣后退） | ✅ [src/quota.py](src/quota.py) |
| 完整前端 | ✅ 首页 · 问卷 · 搜索打分 · 推荐结果 · 问答 · 个人中心 |

库占用 **1,001 MB**（已升级付费计划）· `alias` **263,690 行** ·
测试 **329 项**（`uv run --group etl python -m pytest tests/ -q`）。

---

## 接下来要做的

六件事，互不阻塞。按「现成的东西有多少」排序，不是按重要性。

### 1. 季度更新

管道本来就幂等、靠 md5 跳过未变行，`fetch_moegirl.py` 也一直在存萌娘的
`lastrevid` —— 所以**更新已有作品几乎是免费的**。真正的阻塞在准入：
`favorite.done >= 50` 从构造上就把新番挡在门外。

实测：库内最晚 `air_date` 是 **2026-09-11**，未来要播的只有 **2 部**，
而 `air_year >= 2027` 的 **6 部全都没有 `air_date`** —— 所以「未来 N 个月内开播」
这条并列规则必须处理 NULL，否则会漏掉它正是为之而写的那一批。

动手前有两个决策：准入规则（它会改变候选集，而候选集是所有评测的唯一事实来源），
以及 tag / staff 词表是按季度冻结还是每年重建。冻结会静默忽略新 tag；
增长则会改变向量维度、逼出一次全库重算。

### 2. 信息增益选题

现在的选题按多样性挑（MMR）。下一步是挑「答案最能降低口味不确定性」的那道题 ——
让十道题问出比现在十道题更多的信息。

### 3. 详情页

`GET /api/anime/{id}` 从第 2 周就在服务了，只是前端没有任何页面调它。
这是第 2 周欠账清单里唯一没补上的。

### 4. 英文支持

已经有一部分，而且是**实测过的不是猜的**。档期路由已经认英文
（`ten years ago`、`summer of 2016`）。8 条英文探针里 `resolve()` 正确认出了 4 个
作品或角色。

三个真实缺口：

- 声优触发词全是中文，"Who voices Mikasa?" 会掉到剧情问答去。
- `MENTION_MAX = 16` 比 `fullmetalalchemist`（18 字符）短。
- **33 个常见英文词里有 14 个撞上真实别名** —— `protagonist`、`comedy`、`school`、`key` 都是库里合法的角色名或作品名。于是描述性的英文句子会假命中到某部作品、落进 `AMBIGUOUS`，而找番兜底只在 `UNKNOWN` 时触发，永远够不着。

语料是中文的，所以**跨语言检索惩罚必须先量再定方案** —— 当初「中文查询打日文语料」
实测是 −27.7 个百分点，这是同一个形状的问题。把 150 题自动标注评测集翻成英文重跑
就是一次干净的 A/B，几乎零成本。**先做这个**，它决定了入站翻译到底要不要做。

### 5. 单集简介

dump 里有 **108,835 条分集简介**（中位 193 字，涉及 10,630 部作品），一条都没灌 ——
现在连 episode 表都还没有。它能回答「第 12 话讲了什么」（目前完全答不了），
而且是权威来源，不像萌娘的各话表只有标题。

### 6. 声优岗位进推荐特征

`staff_vec` 覆盖了导演、音乐、制作公司，但**没有任何一部作品带声优字段** ——
声优不在 `subject-persons` 里，要走 `subject-characters` + `person-characters`
两跳。数据其实已经有了（`voice_role` 145,306 行，当初为问答功能建的）。
加进去会改变词表规模，所以 `sql/006` 的列宽和 `staffvec.DIM` 要一起改、
全库重算 —— 这也是它不能顺手做的原因。

---

## 路线图

| 周 | 内容 | 状态 |
|---|---|---|
| 1 | 数据层：dump → 候选集 → 灌库 → tag 清洗 | ✅ |
| 2 | P0 打分、问卷、续作折叠、API、pgvector、前端 v0、部署 | ✅ |
| 3 | Qwen3 embedding · P1 融合 staff/studio · 问卷选题多样化 | ✅ |
| 4 | 萌娘语料 · 角色语料 · 语料转中文 · LLM 选型 · 检索层 | ✅ |
| 5 | **离线评测 —— 这个项目的核心** | ✅ |
| 5.5 | 解析器修复 · 角色页 · 声优配役 | ✅ |
| 6 | 账号 ✅ · 前端 ✅ · 信息增益选题 ⬜ · 季度同步 ⬜ | 🔄 |

第 5 周是让这个项目区别于 demo 的部分。最主要的产出是一个它本来就为之而建的分离：
**资料里确实有答案时，模型答对率 93.8%；而资料里有答案的只有 50.0%。**
瓶颈在检索与语料覆盖，不在生成。两轮语料与检索改动把后一个数字从 30.8% 提到了 50.0%。

第 3 周验证了立项时的那个假设。有 142 部作品的 tag 向量**全为零** ——
以欧美动画和国产老动画为主，官方题材标签对非日本作品明显更稀疏 ——
它们在 tag 余弦下**永远无法被召回**。embedding 救回了其中 **131 部（92%）**，
只剩 11 部。具体到一个案例：《大闹天宫》的最近邻从《修罗武神》《长生界》
这类现代网文改（它的 tag 只有 `玄幻` + `小说改`），变成了
《金猴降妖》(0.79)、《西游记》(0.78)、《人参果》(0.73) —— 全是上美影的西游题材。
这种可复现的前后对照，比一个孤立的 NDCG 数字更能说明问题。

⚠️ **灌 chunk 建议分批 + 每批后跑普通 `VACUUM`，不要 `VACUUM FULL`。**
它重写整张表，而在 Neon 上这些 WAL 计入 instant-restore 存储。
升级付费后这只是成本提示，不再是硬悬崖 —— 超限不会挂起项目了。
**真正的成本变量是 compute（$0.105/CU-小时，上限压到 0.5 CU），不是存储。**

---

## 技术栈

| 层 | 选型 |
|---|---|
| 数据处理 | Python 3.12 · orjson · [bgm-tv-wiki](https://github.com/bangumi/wiki-parser-py) |
| 数据库 | Neon Postgres 18.4 + pgvector 0.8.1（`us-east-2`） |
| 中文分词 | jieba —— Neon 装不了 `zhparser`，BM25 只能在 Python 侧预分词 |
| Embedding | Qwen3-Embedding-0.6B（硅基流动）· `halfvec(1024)` —— **锁死，绝不更换** |
| 重排 | `BAAI/bge-reranker-v2-m3` —— 可以换，它的输出是相对排序 |
| 生成 | Qwen3-14B（回答）· Qwen3-8B（意图分类、声优文案） |
| 认证 | argon2id + PyJWT，放 httpOnly cookie，同源 |
| 环境管理 | uv |
| 后端 | FastAPI on **Vercel serverless**（理由见下「为什么不用常驻进程」） |
| 前端 | React + TypeScript + Vite + Tailwind on Vercel |

⚠️ **embedding 是全项目唯一不能 fallback 到另一个厂商的组件。**
它的输出是**相对于库里那批向量的坐标**；混用两个编码器会得到一个看起来完全正常、
排序也像模像样的噪声列表，而且不报任何错。LLM 和 rerank 没有这个约束 ——
它们的输出是文本和相对排序，用完即弃。真的断供了，正确的降级方向是**退回 BM25**，
不是换一个编码器。

---

## 快速开始

### 1. 环境

```bash
uv sync                       # 只装运行时依赖 —— 线上跑的就是这一组
uv sync --group etl           # + bgm-tv-wiki / tqdm / lxml，scripts/ 要用
uv sync --group api --group etl --group dev   # 开发全量
```

⚠️ **主依赖组是刻意精简的：它等于线上 function 真正需要的东西。**
Vercel 的 Python runtime 检测到 `pyproject.toml` + `uv.lock` 就装**这一组**，
`requirements.txt` 会被完全忽略。任何只在 ETL 用的包都必须待在 group 里，
否则会被打进 function bundle。所以跑 `scripts/` 要加 `--group etl`。
（顺带查出 polars 全仓库零 import，已整个删除。）

### 2. 配置

```bash
cp .env.example .env
```

- `DATABASE_URL_DIRECT` —— 建表和批量灌数据用。走直连，避开 PgBouncer 与 psycopg3 prepared statement 的冲突
- `DATABASE_URL` —— 线上用，走连接池（主机名多一段 `-pooler`）。本地可不填，代码会退回直连
- `SILICONFLOW_API_KEY` —— embedding / rerank / 生成**共用这一个 key**
- `AUTH_SECRET` —— 会话签名。**至少 32 字符，缺失时应用直接启动失败、绝不退回默认值** —— 有默认值的话，忘配的部署会正常启动、正常签发 token，而那些 token 用的是一个公开在源码里的密钥
- `CORS_ORIGINS` —— **只在本地开发有用**，默认放行 Vite 的 5173。⚠️ 线上不要配：前后端同源，CORS 中间件根本不参与。线上若报跨域，说明前端把请求打到了别的域名 —— 该查 `web/src/api.ts`，不是这个变量

⚠️ **部署只需要其中三个：`DATABASE_URL`、`SILICONFLOW_API_KEY`、`AUTH_SECRET`。**
**不要**把 `DATABASE_URL_DIRECT` 配到线上 —— 请求路径上没有任何代码读它，
而且连接池写的是 `DATABASE_URL or DATABASE_URL_DIRECT`：两个都配的话，
前者哪天被拼错就会**静默退回直连**，服务照常起来、功能看着正常，
只是在悄悄耗尽 Neon 的连接数。三个变量各有一条独立验证路径：
`/api/health` 回 `catalog_size` → 库通；注册一个账号 → `AUTH_SECRET` 通；
问一条剧情问题 → API key 通。

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
uv sync --group etl                          # scripts/ 要这一组
psql < sql/001_init.sql
psql < sql/002_tag_vec.sql                   # 别漏：加 tag_vec / series_root 两列
psql < sql/003_vec_halfvec.sql               # vec: vector(1024) → halfvec(1024)，幂等
psql < sql/004_build_meta.sql                # 别漏：build_embeddings.py 会前置检查它
psql < sql/005_mmr_rank.sql                  # 问卷选题的多样性排序列
psql < sql/006_staff_vec.sql                 # P1 的 staff/studio 向量列
psql < sql/007_plot_chunk.sql                # 语料三张表
psql < sql/008_translation.sql               # 译文备份表
psql < sql/009_voice_role.sql                # person / voice_role
psql < sql/010_auth.sql                      # app_user / user_rating / ask_log

uv run python scripts/build_id_map.py        # 需要联网，会下 bangumi-data
uv run python scripts/load_profiles.py
uv run python scripts/backfill_staff.py
uv run python scripts/backfill_anilist.py    # 需要联网，约 125 次请求
uv run python scripts/build_series_map.py
uv run python scripts/build_tag_vectors.py   # 依赖上一步
uv run python scripts/build_embeddings.py    # 需要 .env 里的 SILICONFLOW_API_KEY
                                             # 约 12 分钟 / ¥0.19（缓存命中则秒完成）
uv run python scripts/build_staff_vectors.py # staff_vec + data/interim/staff_vocab.json
uv run --group ml python scripts/build_clusters.py       # mmr_rank + cluster_id，依赖 vec
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
uv run --group etl python -m pytest tests/ -q     # 验收：329 项测试应全绿
```

脚本都幂等，可重复执行。**最后两步不是可选的** —— 跳过 VACUUM 会让库虚涨一倍；
跳过 pytest 就没人发现 `tag_vec` 是否漏跑，而它没跑的话打分**静默返回空列表**，不报错。
`GET /health` 的 `with_tag_vec` 字段也是为此存在。

⚠️ **跑测试要带 `--group etl`。** 其中 28 项调了 `pytest.importorskip("lxml")`，
没有那一组时它们是**被跳过而不是失败** —— 于是「全绿」可能只是没跑。
看跑完那行有没有 `skipped`。

⚠️ **`SILICONFLOW_API_KEY` 是新机器唯一需要人工去申请的东西**（见 `.env.example`），
没有它 `build_embeddings.py` 会在花钱之前就报错退出。
`data/interim/embed_cache/` 不入 git（约 50 MB），所以换机器要重新花 ¥0.19；
**但如果手上有旧机器的缓存文件，拷过去就是零成本重建且 bit-identical** ——
编码器是远程 API 时，这是唯一能保证两台机器建出同一个库的办法。

⚠️ **`data/interim/translate_cache/`（50 MB）是 43,932 条译文的唯一副本。**
丢了是重翻 8 小时；而编码缓存丢了只是 ¥0.19 和 12 分钟 —— 量级完全不同，
这一份要单独备份。

### 5. 跑起来

```bash
uv run uvicorn server.main:app --reload      # API，文档在 /api/docs
cd web && npm install && npm run dev         # 前端 :5173，/api 自动代理到 :8000
uv run --group etl python -m pytest tests/ -q     # 改过任一条打分路径后必跑
uv run ruff check src/ scripts/ server/ tests/
cd web && npx tsc --noEmit && npm run lint   # 前端检查
```

> **Windows 注意**：往终端打中文的脚本要加 `PYTHONIOENCODING=utf-8`。
> API 不需要（它吐 JSON），`scripts/try_questionnaire.py` 也不需要（它自己处理编码）。

---

## 接口

所有路由都在 `/api` 下。

**推荐这条链路是无状态的** —— 评分随请求传入，服务端零写入。
游客的 localStorage 和登录用户喂进同一个入口；账号层是**并排**在它旁边的，不在它里面。

| 接口 | 用途 | 模型调用 |
|---|---|---|
| `GET /health` | 存活探针 + 五个回填覆盖率字段 + 分词指纹 | — |
| `GET /questionnaire` | 选题（`n` / `experience` / `include_nsfw` / `fold_sequels`） | — |
| `POST /recommend` | 打分（`answers` / `mode` / `rank_by` / `min_score` …） | — |
| `GET /search` | 按名/别名搜 —— BM25，拼错时退到 pg_trgm 兜底 | — |
| `GET /anime/{id}` | 详情 | — |
| `GET /season` | 按档期浏览 | — |
| `GET /related` | 同作者 / 导演 / 公司的其他作品 | — |
| `GET /voice` | 某声优配过哪些角色 | — |
| `GET /find` | 按描述语义找番 | 1 次 embedding |
| `POST /ask` | **单一入口** —— 分派到剧情问答 / 声优 / 档期 / 找番 | 2+ |

**账号：** `POST /auth/register`、`/auth/login`、`/auth/logout`、
`GET /auth/me`、`PUT /auth/username`、`PUT /auth/password`、
`GET|PUT|DELETE /ratings`、`GET /ratings/detail`。

⚠️ **「游客能用」与「要登录」的判据是这个端点会不会花钱，不是它像不像问答。**
`/voice`、`/season` 是纯 SQL，虽然属于问答但对游客开放；
`/find` 只调一次 embedding，但仍然要登录 —— 不拦的话它就是**绕过配额的后门**，
同一件事换个 URL 就免费了。

前端传**作答选项**，不传算好的分数：

```json
{"answers": [{"subject_id": 243916, "choice": "seen", "score": 9},
             {"subject_id": 328609, "choice": "wish"}],
 "mode": "all", "rank_by": "blend", "top_k": 10}
```

选项 →（分数, 置信度）的映射只在服务端一处维护 —— 让前端算等于把它复制进
TypeScript，一漂移就是静默的推荐质量下降。同一条纪律也是**库里存 choice 而不存
算好的分数**的原因：把今天的映射固化进行里，将来调映射就得重写全表，
而在那之前历史行会带着旧口径静默污染推荐质量。

---

## 目录结构

```
src/
  candidates.py      候选集口径 —— 唯一事实来源
  tag_rules.py       tag 分类规则表 + 同义合并表 + 导入时自检
  tagvec.py          tag 向量的唯一计算实现（log1p × idf × L2）
  recommend.py       内存打分 —— 离线评测用
  recommend_sql.py   Postgres 打分 —— 线上用，必须与上面逐条等价
  questionnaire.py   选题 + 作答→评分映射
  textproc.py        jieba 分词 + 词典指纹
  embed.py           embedding 模型的唯一定义处 —— 锁死、带指纹
  retrieve.py        检索管道 + 实体解析状态机。只读库
  rerank.py          重排客户端 —— 可以换模型，与 embed.py 不同
  llm.py             生成、意图分类、prompt、配置指纹
  router.py          意图分派 —— 纯函数，零模型、不碰库
  find.py            语义找番
  voice.py           声优配役查询
  related.py         结构化关联查询
  auth.py            密码哈希、会话 token、用户名归一化
  ratings.py         评分持久化 + 游客数据合并
  quota.py           问答配额 —— 先扣后退，5xx 才退
server/              FastAPI 应用 —— 所有路由都在 /api 下
web/                 Vite + React + Tailwind 前端（同一个 Vercel 项目）
  session.tsx        会话与评分的**单一入口**，见下
api/index.py         Vercel 入口 —— 这个目录**只能放这一个文件**
sql/                 001 建表 · … · 007 语料 · 009 声优 · 010 账号
scripts/             ETL 与回填，各管各的列，绝不交叉
tests/test_parity.py 断言两条打分路径逐条一致
```

🚨 **`web/src/session.tsx` 是前端最该先读的文件。** 上层组件**不知道用户登没登录** ——
它们拿到的永远是 `{answers, setAnswer}`，数据存 localStorage 还是同步到账号由它决定。
这是服务端那条「评分随请求传入」铁律在前端的对应物。
一旦让某个组件自己写 `if (user) … else …`，这条铁律就会在多处同时破掉、各自漂移。

---

## 数据集口径

```
type == 2                                    # 动画
AND 有放送年份                                # date 为空时回退到 infobox
AND meta_tags ∩ {TV, WEB, 剧场版, OVA} != ∅   # 排除短片和无形态标签的同人/MV
AND favorite.done >= 50                      # 质量门槛
→ 11,453 部
```

口径定义在 [src/candidates.py](src/candidates.py)。改口径**只改这一个文件**。

`done >= 50` 一次清零了三个数据质量问题 —— 无 tag、无评分、无人看过。
它同时也是新番进不来的原因，正是上面「季度更新」那条待决策的点。

---

## 为什么不用常驻进程

打分的设计（11,453 × 308 矩阵，每请求一次矩阵乘法）本来需要一个长命进程。
serverless 没有：每请求重建矩阵要传 2.6 MB、耗 1.31 s，而打分本身只要 12 ms。
反直觉的是，**低流量会让冷启动更糟而不是更好** —— 作品集项目访问零星，
大部分请求都会撞上冷容器。

于是把余弦推进了 Postgres。每请求只拉用户评过的那几十部（约 70 ms，几乎全是网络往返），
11,311 行的暴力余弦由 pgvector 算 —— 实测**成本 ≈ 0 ms**，
印证了此前「不建 HNSW 索引」的判断。

代价是**两套打分实现**：线上 SQL，离线 leave-one-out 评测用 numpy
（10⁵–10⁶ 次打分，走往返做不到）。同一个公式两份实现，
正是设计原则里禁止的「两套口径」，所以一致性不靠纪律维持，而靠两条构造上的保证：

1. `anime_profile.tag_vec` 是向量的**唯一**定义处，两条路径读同一批数字（实测逐位相同）
2. [tests/test_parity.py](tests/test_parity.py) 在所有排序模式、时间窗口、开关组合下逐条比对输出

第 2 条不是多余的。它立刻抓出了 SQL 召回池少写的一个次级排序键：
由于大量作品与给定偏好向量**精确正交**，候选池是一大片并列 ——
两条路径召回的**根本不是同一批候选**，而两边看上去都完全合理。

---

## 已知的坑（都踩过并验证）

**中文 BM25 不能直接用 Postgres tsvector。** 内置分词器把整句中文当一个 token，
而 Neon 装不了 `zhparser`/`pgroonga`。必须在 Python 侧用 jieba 预分词后再入库。
**建库与查询必须用同一分词器 + 同一词典**，否则召回直接崩，而且是静默的。
应用启动时会校验词典指纹。

**`alias` 的唯一约束必须写 `NULLS NOT DISTINCT`。** 表里 `subject_id` 和
`character_id` 必有一个是 NULL，而 Postgres 默认把 NULL 视为互不相等 ——
不加这句，约束对每一行都失效。这个坑后来换个位置复发了一次：声优行**两个 id 都是
NULL**，所以 `person_id` 必须也加进唯一约束，否则两个同名声优会撞约束、
后灌的被静默丢掉。

**serverless 的连接池必须配 `check=ConnectionPool.check_connection`。**
Neon 会回收空闲连接，而 psycopg 的池不知道；下一个请求拿到那条死连接就 500。
线上少见只因为流量近乎为零、几乎每个请求都落在新容器上 ——
真正的触发条件是「容器还活着但连接已被回收」。
⚠️ 注意 `pg_terminate_backend` **复现不出**这个故障（它发 RST，socket 层立刻可见），
必须真晾满空闲窗口。

**`date` 为空不等于该丢弃。** 213 部仅缺 `date` 的条目里，97% 的日期就在 infobox 里，
而且几乎全是国产经典（大闹天宫、葫芦兄弟、黑猫警长）。

**Tag 里最大的噪声不是「神作」「补番」这类情绪评价，而是结构化信息错位。**
制作公司和人名加起来比题材 tag 还多。这些不该丢弃，
而应分流去结构化的 `studios`/`staff` 字段。

**扩大候选集必须重跑规则审查。** 从 2011+ 扩到全年份后，一次性冒出 50+ 个新漏网条目：
老一辈监督、老 IP、繁体变体（`裡番`/`里番`）、怀旧元评价。换个年代就是换一套词汇。

**评分下限比评分上限有用。** 高分不保证好看，低分却几乎必然难看。
排除 78 部低于 3.5 的作品（占全库 0.68%）几乎没有代价，
却挡住了一个具体的失败模式：烂续作的 tag 与你喜欢的那一季几乎相同，
于是 tag 余弦把它排到第一 —— 实测 `match=0.983`。

**「资料里没有」往往只是你看不见它。** 两次同源。作品自己的 Bangumi 简介 ——
问「讲了什么故事」时最权威的那份文本 —— **从来不在检索池里**。
而打分表只渲染检索到的 chunk，别处注入的资料一直没显示过：
打分的人看到「资料里没有」却发现模型答出来了，会判成幻觉，
而那其实是有出处的真资料。**问「答案为什么不在资料里」之前，
先问「资料清单是不是完整的」。**

**指标会从自己的定义下面漂走。** 评测把「未命中却给了答案」判为幻觉、
把「拒答」判为没回答。后来一次 prompt 改动**有意**把干巴巴的拒答变成有据的部分回答 ——
于是幻觉率从 22.2% 涨到 84.6%，而把那 26 道逐条读完，**没有一道是编造的**。
量具已经不再测量它名字所声称的东西，而那个数字本身看起来完全正常。

**分数有噪声时，规则完胜调阈值。** rerank 分的批间噪声约 1e-3，
而 OP/ED 类 chunk 本身就只有 0.003–0.028 —— 任何绝对地板都分不开它和垃圾。
给最佳歌曲 chunk **占一个席位**，7 道失败题修好 5 道，代价是每题上下文 +0.09 条；
而把地板降到 0 只修好 4 道，却要 +3.44 条。**便宜 38 倍，而且不会漂。**

**`api/` 目录下的任何文件都会变成一个独立的 Vercel function。**
所以应用包放在 `server/`，`api/` 只留一个入口文件。把 `schemas.py` 放进去会导致构建失败。

**Vercel 装的是主依赖组，不是 `requirements.txt`。** 它的 Python runtime 检测到
`pyproject.toml` + `uv.lock` 就用 uv 装**主依赖组**，手工维护的 `requirements.txt`
被静默忽略。第一次部署因此装了 polars 却没装 FastAPI。修法不是改部署设置，
而是**把主依赖组当作部署清单**。凡是在请求路径上的包 —— `httpx`、`argon2-cffi`、
`pyjwt` —— 都必须在主依赖组里，否则线上会**在模块级 import 就炸**，
把整个 ASGI app 一起带走。

---

## License

See [LICENSE](LICENSE).

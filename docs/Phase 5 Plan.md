# Phase 5 Plan — Backend API

Nov 16, 2026. The service around the Phase 4 agent. No UI in this phase: every behaviour
the Phase 6 frontend needs is built and tested here first, against the API directly.

## Goal

A lawyer's contract goes in as a PDF, DOCX or pasted text; a clause-level review comes out,
streamed step by step, grounded, traced, and stored — and the same work is never paid for
twice.

**Gate:**

- a full AGB PDF posted to `/api/v1/documents` and reviewed via `/api/v1/reviews` streams
  every in-scope clause's verdict as AG-UI events
- the whole review is **one Langfuse trace**, one span per clause, the agent's steps nested
- uploading the same file again makes **zero LLM calls** and returns the stored result
- a client that disconnects and reconnects with `Last-Event-ID` misses no event
- a 👎 with a correction posted to `/api/v1/feedback` lands as a score on the right step
- `make check` green, including the new API, ingest and runner tests (fakes, no network)

## Principles

1. **Content-addressed everything.** Documents, clauses, verdicts, retrievals and
   embeddings are keyed by a content hash plus the version of whatever produced them
   (`parser_version`, `splitter_version`, `agent_version`, index hash). No time-based
   expiry: a new version is a new key, old rows stop matching, nothing is overwritten.
2. **The agent judges one clause and knows nothing else.** No HTTP, no documents, no
   reviews inside `app/agent`. Orchestration lives in `app/review`.
3. **A failure belongs to its clause.** One clause failing never fails the review.
4. **Postgres is the source of truth for runs.** Events are stored before they are sent,
   so reloads, reconnects and restarts lose nothing. No Redis in v1.

## Architecture

```
React (Phase 6) ─HTTP/SSE─► app/api      routes, request models, SSE encoding
                                │
                                ▼
                            app/review   start, dedupe, run cache-first, limit, emit, resume
                      ┌─────────┼──────────────┐
                      ▼         ▼              ▼
                 app/ingest  app/agent     app/store   Postgres repositories + file store
                      │         │              │
                      └─────────┴──────┬───────┘
                                       ▼
                     packages/core  (config, schemas, hashing, llm, budget,
                                     retrieval, routing, db, langfuse_client)
                                       │
          Gotenberg · Gemini · Jina · Qdrant · Neon Postgres · Langfuse (all EU)
```

An import-linter contract inside `app` keeps `api → review → {ingest, agent, store}` and
forbids `agent` from importing `api`, `review` or `store`.

### Module layout

```
app/
  api/       main.py (startup: config, pool, migrations, statute cache, limiter, resume)
             deps.py  schemas.py  sse.py
             routes/  documents.py reviews.py feedback.py decisions.py statutes.py health.py
  ingest/    validate.py convert.py parse.py split.py
  review/    service.py runner.py limiter.py events.py tracing.py
  store/     migrations/*.sql  documents.py clauses.py verdicts.py reviews.py
             events.py feedback.py files.py
  agent/     existing graph
```

### Tech stack

| Concern | Choice |
| --- | --- |
| HTTP | FastAPI, uvicorn, Pydantic v2, `python-multipart` |
| Agent ↔ UI protocol | AG-UI via `ag-ui-protocol`, over server-sent events |
| Agent | LangGraph (Phase 4), run in a worker thread per clause |
| PDF text and coordinates | `pdfplumber` (MIT; PyMuPDF rejected for its AGPL licence) |
| DOCX → PDF | Gotenberg (headless LibreOffice in Docker, HTTP API) |
| Database | Neon Postgres via psycopg 3, numbered SQL migrations |
| Files | `FileStore` interface: local disk now, EU object storage in Phase 9 |
| Tracing and feedback | Langfuse SDK |
| Tests | pytest, FastAPI TestClient, fake LLM / retrieval / store clients |
| Local run | Docker Compose (API + Gotenberg), `make api` |

## Tasks

### 5.A · Store and migrations

**Goal:** tables shaped for content addressing, so caching and audit fall out of the
schema instead of being bolted on.

**Implementation.**

| Table | Key | Holds |
| --- | --- | --- |
| `uploads` | `upload_id` | document, filename, type, size, uploaded by/at — one row per upload event |
| `documents` | `document_id` = sha256 of the file bytes | stored PDF path, page count, `parser_version`, `splitter_version`, text hash |
| `document_clauses` | (`document_id`, `position`) | number (`6.1`), heading, `content_hash`, page, line rectangles (JSON), in-scope flag |
| `clause_texts` | `content_hash` | clause text, stored once however many documents contain it |
| `verdicts` | (`content_hash`, `agent_version`) | verdict, legal basis, reason, confidence, trace id, step observation ids, tokens, latency |
| `retrievals` | (`content_hash`, index hash, policy hash) | ranked candidates |
| `embeddings` | (`content_hash`, model) | clause vector |
| `reviews` | `review_id` | document, `agent_version`, scope, status, counts, timings, cost |
| `run_events` | (`review_id`, `seq`) | every AG-UI event as JSON |
| `decisions` | (`review_id`, `position`) | accept / override / dismiss, corrected verdict and §, note, who, when |
| `feedback` | stable `feedback_id` | review, clause, `agent_version`, step, observation id, vote, reasons, correction, comment |

- `verdicts` is **re-keyed** from the evaluation clause id (`agb-1039`) to `content_hash`;
  `lawyer_decision` moves to `decisions`, because a decision belongs to one review, not to
  every document sharing the clause. The eval harness switches to the content hash too.
- The evaluation `clauses` table is untouched: it stays the answer key, and app tables are
  separate, so labels still cannot reach the agent.
- Migrations are numbered SQL files applied at startup and recorded in a
  `schema_migrations` table.

**Done when:** migrations apply to an empty database and to the current one; repository
tests cover insert-or-reuse on every content-addressed table.

### 5.B · Ingest: file to clauses with positions

**Goal:** turn a real contract into clauses the agent can judge and the UI can highlight.

**Implementation.**

- `validate.py` — size limit (20 MB), type checked by content not extension, reject
  encrypted PDFs and PDFs without a text layer with a message the UI can show.
- `convert.py` — DOCX → PDF via Gotenberg, with a timeout. Macros never execute.
- `parse.py` — pdfplumber words with page and coordinates; runs in a subprocess with time
  and memory limits (PDF bombs).
- `split.py` — segmentation on German AGB numbering (`§ 6`, `6.1`, `(1)`, `a)`) and
  headings, joined across lines and pages; each clause keeps its rectangles.
  `SPLITTER_VERSION` is bumped on any change.
- Pasted text skips conversion and parsing; clauses then have text offsets, not rectangles.
- Parse cache: the same file bytes with the same parser and splitter versions return the
  stored clauses without parsing.
- **A splitter test set:** 5–10 public shop-AGB PDFs and one DOCX with hand-marked clause
  boundaries in `data/gold/`. The AGB-DE eval set arrives pre-split, so nothing else
  measures this — and a bad split makes every downstream verdict wrong.

**Done when:** the splitter matches the hand-marked boundaries on the test set (target:
95% of clauses exact), and a second upload of the same file does no parsing.

### 5.C · Review runner

**Goal:** judge a document's clauses fast, cheaply and without one failure stopping the
rest.

**Implementation.**

- `service.start_review()` computes `agent_version`; if a run for the same document and
  version is active it is returned instead of starting another (in-flight dedupe).
- `runner` walks in-scope clauses in document order with up to `max_concurrency` in
  flight. For each: verdict cache hit → emit and continue; miss → acquire from the limiter,
  run the graph in a worker thread, persist, emit.
- Embedding and retrieval caches in `core.embeddings` / `core.retrieval`, keyed as in 5.A.
- `limiter.py` — **one** rate limiter and daily request budget for the whole server
  (today's `core.budget` is per run, so two reviews at once would exceed the Gemini
  limit). Out of budget means the clause waits, and the UI is told why.
- Retries with backoff on 429 and 5xx; a timeout on every external call.
- A failed clause is marked `failed` with its error and the review carries on;
  `POST …/clauses/{pos}/retry` re-judges it.
- If the explanation call fails, the verdict and basis are kept with "reasoning
  unavailable".
- On startup, reviews still `running` are resumed; the verdict cache makes that cost only
  the unjudged clauses.

**Done when:** fake-client tests prove cache hits make no calls, one failing clause leaves
the others done, two concurrent reviews share one budget, and a killed run resumes.

### 5.D · Events, API and tracing

**Goal:** the client sees every step as it happens and can always catch up.

**Implementation.**

- `events.py` appends each event to `run_events` with an increasing `seq`, then fans it
  out to connected listeners. `GET /reviews/{id}/events` replays from `Last-Event-ID`, then
  follows live.
- AG-UI events: `RUN_STARTED`; `STATE_SNAPSHOT` (all clauses and their status, at start
  and on reconnect); `STEP_STARTED` / `STEP_FINISHED` per agent step with a short summary;
  `STATE_DELTA` when a clause finishes; `CUSTOM progress` (counts, cache hits, cost, budget
  left); `RUN_ERROR`; `RUN_FINISHED`. Each carries a schema version.
- Endpoints under `/api/v1`:

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/documents` | upload or paste; returns the document and its clauses (or the existing one) |
| `GET` | `/documents` | list for the home screen, with latest review counts and progress |
| `GET` | `/documents/{id}` | metadata and clauses with positions |
| `GET` | `/documents/{id}/file` | the PDF, immutable cache headers |
| `POST` | `/reviews` | start a review (or return the running one) |
| `GET` | `/reviews/{id}` | status, counts, cost, cache hit rate |
| `GET` | `/reviews/{id}/findings` | all clause results with decisions — the reload path |
| `GET` | `/reviews/{id}/events` | AG-UI over SSE with replay |
| `POST` | `/reviews/{id}/clauses/{pos}/retry` | re-judge a failed clause |
| `POST` | `/feedback` | vote, reasons, correction |
| `PUT` | `/reviews/{id}/clauses/{pos}/decision` | accept / override / dismiss + note |
| `GET` | `/statutes/{chunk_id}` | statute text, served from memory |
| `GET` | `/health`, `/ready` | liveness; dependencies reachable |

- Tracing: one Langfuse trace per review, one `agent` span per clause, the existing step
  observations nested beneath. `graph.py` accepts a parent span and returns the
  observation ids of its judge, retrieval and explanation steps, stored on the verdict.

**Done when:** a TestClient run streams a full fake review; a reconnect mid-run with
`Last-Event-ID` receives exactly the missed events; a live 3-clause run shows one nested
trace.

### 5.E · Feedback and decisions

**Goal:** every vote and decision reaches both Langfuse and our own records, attributed to
the agent step it is about.

**Implementation.**

- `POST /feedback` takes the vote and, on 👎, one or more reasons, each mapped to a step:

| Reason | Step scored |
| --- | --- |
| Verdict wrong (+ correct verdict) | judge |
| Wrong or missing § (+ correct §) | retrieval |
| Reasoning unclear or wrong | explanation |
| Clause cut incorrectly | splitter (no trace step; stored, reported separately) |
| 👍 | the clause span |

- Each becomes a Langfuse score on that observation, with a stable score id so a changed
  vote updates rather than duplicates, plus a `feedback` row with the `agent_version`.
- `PUT …/decision` stores accept / override / dismiss and a note; accept and override are
  also recorded as feedback, so Phase 7 reuses one mechanism.

**Done when:** a fake-Langfuse test shows each reason scored on the right observation, and
re-posting the same feedback updates one score.

### 5.F · Hardening and operations

**Goal:** safe to run with real contracts in it, and diagnosable when something breaks.

**Implementation.**

- **Prompt injection from the contract:** the judge asks fixed typed questions on the
  Decisions API, so clause text cannot change the questions or invent a citation. The
  explanation prompt treats the clause strictly as quoted data; its output is length-capped
  and returned as plain text.
- Upload hardening from 5.B; CORS limited to the frontend's origin; request limits on
  upload and review endpoints; secrets only in the environment.
- Structured JSON logs carrying `review_id`, `document_id` and the Langfuse `trace_id`.
- `/ready` checks Neon, Qdrant, Gemini and Jina.
- Docker Compose (API + Gotenberg), `make api`; config in `configs/app.yaml` (upload
  limit, daily budget, `explain_enabled`).
- Deletion is designed now, built in Phase 9: removing a document may only remove clauses
  no other document references.

**Done when:** a malformed PDF, an oversized file and a DOCX with macros are each rejected
or handled with a clear error, and one request can be followed from log line to trace.

## Changes to existing code

1. `verdicts` re-keyed on `content_hash`; `lawyer_decision` moved to `decisions`; eval
   harness follows.
2. `app/agent/graph.py`: accepts a parent span, returns step observation ids.
3. `core.embeddings` / `core.retrieval`: cache lookups.
4. `core.budget`: a shared, thread-safe limiter with a daily budget.
5. `pyproject.toml`: `pdfplumber`, `python-multipart`, `ag-ui-protocol`.

## Out of scope

Login and accounts, OCR for scanned PDFs, the graph interrupt on `uncertain` (Phase 7),
exports (Phase 8), PII masking and object storage (Phase 9), Redis and separate workers.

## Scaling later, without Redis

Reviews become rows in a Postgres job queue claimed with `FOR UPDATE SKIP LOCKED`; live
events cross processes via `LISTEN/NOTIFY` (`run_events` is already the source of truth);
the limiter becomes a Postgres counter; files move to EU object storage behind `FileStore`.

## Open decisions

1. **PDF library:** pdfplumber (recommended, MIT) or PyMuPDF (faster, AGPL).
2. **File storage:** local disk now (recommended), EU object storage before real client
   documents (Phase 9).
3. **Login:** none in v1 (recommended); the audit columns exist so "who" can be filled in
   once accounts do.

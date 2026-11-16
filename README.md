# AGB-Check

Agentic review of German consumer terms and conditions (AGB): flags potentially void
clauses, cites the BGB paragraph, and explains why. Learning prototype in the legal
due diligence area.

## Documents

- **`docs/Metrics.md` - every number the project is judged on, and which two are misleading**
- `docs/AGB-Check Agentic AGB Review for German Legal Due Diligence.md` - project brief, system design, evaluation
- `docs/Data pipelines.md` - P1 knowledge base ingestion, P2 evaluation data preparation
- `docs/Development Phases.md` - build order and the gate for each phase
- `docs/Phase 1 Results.md` - knowledge base numbers, what the spikes settled, limitations
- `docs/Phase 2 Results.md` - clause counts, the flag-not-drop decisions, answer-key caveats
- `docs/Phase 3 Results.md` - the one-shot baseline, and why recall is the unstable half
- `docs/Phase 4 Plan.md` - the agent: what it does, its runnability gate, and the continuous evaluation track
- `docs/Phase 4 Results.md` - what Phase 4 built and the numbers measured so far
- `docs/Phase 5 Plan.md` - the backend API: ingest, cache-first review runner, AG-UI stream, feedback
- `docs/Phase 6 Plan.md` - the frontend: annotated document, findings, table, feedback

## Layout

```text
packages/core/   shared schemas, config, invariants - imported by all, deploys nowhere
pipelines/
  p1_kb/         offline job: builds the BGB vector index
  p2_eval/       offline job: builds the clause table and the Langfuse dataset
app/
  agent/         LangGraph agent
  api/           FastAPI + AG-UI
evals/           runs in CI against app/agent; not a pipeline
configs/         versioned pipeline inputs, hashed into the store manifest
data/            raw -> interim -> processed; only data/gold is committed
stores/          index manifest; vectors in Qdrant Cloud, tables in Neon Postgres
reports/         quality gate output, committed for its git history
```

Boundaries are enforced by `.importlinter`, checked in CI: `app` and `pipelines` are
siblings and may not import each other, pipelines write the stores, the app reads them.

## Getting started

```bash
cp .env.example .env     # fill in the keys; see the comments in the file
make install
make smoke               # Phase 0 gate
```

## Running the agent

The agent needs `GEMINI_API_KEY` (judge and explanation), `JINA_API_KEY` (routing and
retrieval), Qdrant (the BGB index built by `make kb`), `DATABASE_URL` (clauses and
verdicts) and the Langfuse keys (prompts and traces).

```bash
make seed-prompts        # once, and after any prompt change
make eval-smoke          # 5 dev clauses end to end - the Phase 4 gate
make eval                # the stratified 70-clause dev slice, for continuous evaluation
```

Each clause produces one nested Langfuse trace (route, retrieve, judge, self-check,
retry, explain) and a row in the Postgres `verdicts` table, keyed by clause content
hash and `agent_version` and carrying the trace id. The run report lands in `reports/`:
`p4_agent_dev5.json` for the smoke run, `p4_agent_dev70.json` for `make eval`.
Models, thresholds and the retrieval policy live in `configs/app.yaml`; run size and
quota in `configs/evals.yaml`.

## Running the API

The API needs the same keys as the agent (`GEMINI_API_KEY`, `JINA_API_KEY`, `QDRANT_URL`,
`QDRANT_API_KEY`, `DATABASE_URL`) and `GOTENBERG_URL` for DOCX conversion. `LOG_FORMAT=json`
is optional: the API always logs one JSON object per line, with `request_id`, `review_id`,
`document_id` and `trace_id` where known.

```bash
make api-up              # build and start the API and Gotenberg in Docker Compose
make api                 # or: run the API locally with reload, Gotenberg from compose
make api-down            # stop the compose stack; stored PDFs stay in the files volume
```

- `http://localhost:8000/api/v1/health` is liveness and always returns 200.
- `http://localhost:8000/api/v1/ready` checks Neon, Qdrant, Gemini and Jina in parallel,
  each with a two-second deadline. It returns 503 and names the failing check.

The API runs with **one worker** (`--workers 1`). Review runs and their SSE event streams
are held in process memory until they move to Postgres LISTEN/NOTIFY; a second worker
would split a review's events from the clients watching it.

Limits live in `configs/app.yaml`: `ingest.max_upload_mb`, `api.cors_origins` (the only
browser origin allowed), `api.rate_limit_per_minute` and `api.rate_limit_burst` (uploads
and reviews per client IP), and `files.root` for the stored PDFs.

## Current state

Reference numbers, with the caveats that matter, in `docs/Metrics.md`:

| Metric | Value | Where it stands |
| --- | --- | --- |
| Void F1, Phase 3 baseline (dev, 419 clauses) | **0.276** | target for continuous evaluation |
| Retrieval primary@5 (real clauses) | 0.258 → **0.355** | improved in Phase 4 |


Phase 1 complete: `make kb` builds and validates the BGB knowledge base, primary@5 0.933
against the 0.90 gate. See `docs/Phase 1 Results.md`, including the known limitations.

Phase 2 complete: `make eval-data` builds the 523-clause scope slice, splits it 419/104
and publishes the clause table plus the Langfuse answer key; `make eval-data-dry` asserts
every gate and the label-leak invariant without writing to any store; `make recheck`
re-scores retrieval on real clause text.

That re-check is the headline Phase 2 finding: **primary@5 0.258** over 31 real void
clauses, against Phase 1's synthetic 0.933. It is recorded, not gated. The gap decomposes
into a reachability defect in the topic filter and a register mismatch between clauses and
the prohibitions that void them - see `docs/Phase 2 Results.md`. Fixing it is the first
Phase 4 experiment.

Phase 3 complete: `make baseline` runs the one-shot classifier over the dev half and
`make baseline-held-out` produces the gate number. The baseline scores **void F1 0.276
on dev** (P 0.174 / R 0.677) and 0.304 on held-out - it over-calls void roughly five
times for every correct call, so precision is what Phase 4 has to earn. The harness in
`evals/` is permanent and agent-agnostic; Phase 4's graph plugs into it unchanged. See
`docs/Phase 3 Results.md`.

Phase 4: the agentic RAG graph runs end to end - see **Running the agent**. Phase 4
closes on runnability, not on a metric; void F1 and recall are improved as continuous
evaluation alongside later phases. See `docs/Phase 4 Plan.md`.

## Stack

Python 3.12, uv, LangGraph, FastAPI, Qdrant Cloud for vectors, Neon Postgres for
tables, Prefect Cloud for pipeline orchestration and run observability, Jina for
embeddings and topic routing, Gemini for the judge (Decisions API) and the explanation, and Langfuse (Cloud EU) for traces,
prompts, datasets and scores.

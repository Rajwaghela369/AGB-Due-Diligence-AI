# Development Phases

Nov 4, 2026

Build order for AGB-Check. Each phase ends with a recorded number or artifact that the next phase has to beat or consume. Phases 0–4 are strictly sequential.

## Repository shape

One repo, three things that run independently: offline pipelines, the app, and the eval suite.

```text
agb-check/
├── packages/core/        # schemas, config, store clients — imported by all, deploys nowhere
├── pipelines/
│   ├── p1_kb/            # batch job: builds the BGB vector index
│   └── p2_eval/          # batch job: builds clause table + Langfuse dataset
├── app/
│   ├── agent/            # LangGraph graph
│   └── api/              # FastAPI + AG-UI
├── frontend/             # React + CopilotKit
├── evals/                # runs in CI against app/agent; not a pipeline
└── Makefile
```

**Boundary rules**

- `packages/core` is the only shared code; `app/` never imports from `pipelines/`
- Pipelines write the stores, the app reads them
- `evals/` imports `app/`, never the reverse
- The label-leak invariant lives in `packages/core`, asserted by both the P2 publish step and the app's clause loader

## Phases

### Phase 0 · Foundation

Repo skeleton, dependency setup, config loading, logging, Makefile targets. Decide the embedding model (`bge-m3` vs `multilingual-e5-large`) and the LLM (EU-hosted API vs local) — both block later phases. Langfuse reachable.

**Gate:** `make` targets exist and fail with clear messages; a trace appears in Langfuse.

### Phase 1 · P1 KB-INGEST — BGB knowledge base

Scope is the knowledge base only. **AGB-DE is not touched in this phase.**

Stream the Open Legal Data dataset at a pinned revision, snapshot the BGB rows, select the paragraph whitelist, parse the `content` HTML into item-level chunks with deterministic IDs, embed into Chroma collection `bgb_agb_v1`, and write a manifest. Validation uses ~30 hand-written clause-style queries, drafted after paragraph selection and before chunking so they are not fitted to the chunks.

**Gate:** primary@5 >= 0.90 with primary@1/@3 and MRR also reported, written to `reports/p1_retrieval_check.json`. Below that, fix in order: topic tagging, chunk granularity, hybrid/BM25, embedding model.

**Known limitation:** the synthetic gold set is cleaner than real clause text, so this gate proves the index is structurally sound, not that it works on real input. Phase 2 re-runs the same check with real clauses.

### Phase 2 · P2 EVAL-PREP — clauses and answer key

Fetch AGB-DE, normalize, filter to liability and warranty, reuse the authors' train/test split. Publish clauses to Neon Postgres without labels; labels only into the Langfuse dataset.

Also regenerates the retrieval gold set from **train-split** clauses via a `subtopic -> chunk_id` mapping, and re-runs the Phase 1 retrieval check against real clause text. The test split stays untouched until Phase 3.

**Gate:** `reports/p2_counts.json` matches 523 clauses / 39 void; the label-leak check passes; the retrieval re-check is recorded (a drop against Phase 1's synthetic number is expected and informative, not a failure).

### Phase 3 · Baseline and eval harness

One-shot classifier, no retrieval, run as a Langfuse experiment. Scorers for precision, recall and F1 on the void class only. The harness lands in `evals/` and stays there permanently.

**Gate:** baseline F1 recorded in Langfuse, and a CI job that reruns the suite on every change to prompts or the agent.

### Phase 4 · Agentic RAG core

Headless LangGraph graph: topic router (Jina embedding) → retrieve BGB → judge (Gemini Decisions API, one call) → self-check → retry retrieval once → explain (`reason_de`, void verdicts only) → finish. Uncertain verdicts are marked and skipped for now. Prompts versioned in Langfuse, one trace per clause, verdicts persisted to Neon Postgres keyed by content hash and `agent_version`.

**Gate:** the agent is runnable — `make eval-smoke` completes on 5 dev clauses with zero task failures, every void verdict is grounded in a retrieved chunk, each clause has one nested Langfuse trace whose id is on its `verdicts` row, and `make check` is green. See `docs/Phase 4 Plan.md`.

**Quality is tracked, not gated.** Void recall above 0.70 and F1 above the Phase 3 baseline (0.276 dev, 0.304 held-out) are targets for continuous evaluation alongside Phase 5 onwards, compared as Langfuse experiments and recorded in `docs/Metrics.md`. Experimentation is ongoing work, so it does not block the phases after this one.

**Retrieval, first experiment (done).** Phase 2's re-check on real clause text scored primary@5 0.258, against Phase 1's synthetic 0.933, partly because the topic filter made chunks tagged `other` unreachable (see `docs/Phase 2 Results.md`). Always including §§ 305–310 and raising `top_k` to 10 moved it to **0.355**. Hybrid/BM25 for the register mismatch remains a candidate.

### Phase 5 · Backend API

FastAPI service around the Phase 4 agent: upload a PDF, DOCX or pasted text, split it into clauses with their positions on the page, review it with a cache-first runner, and stream every step to the client as AG-UI events. Plan: `docs/Phase 5 Plan.md`.

**Goal:** everything the UI needs, testable without a UI — and never paying twice for the same work.

**Tasks:**

- 5.A Store: new tables (`uploads`, `documents`, `document_clauses`, `clause_texts`, `reviews`, `run_events`, `decisions`, `feedback`, caches), numbered SQL migrations, `verdicts` re-keyed on clause content hash
- 5.B Ingest: validate, DOCX → PDF (Gotenberg), parse with coordinates (pdfplumber), split into clauses, versioned parse cache
- 5.C Review runner: cache-first (verdicts, retrieval, embeddings), one server-wide rate limiter and daily budget, failure isolation per clause, resume after restart
- 5.D Events and API: AG-UI events over SSE with replay, REST endpoints under `/api/v1`, one Langfuse trace per review
- 5.E Feedback and decisions: 👍/👎 with reasons and corrections scored on the right agent step, decision storage
- 5.F Hardening and operations: upload and prompt-injection safeguards, structured logs, health checks, Docker Compose

**Gate:** a full AGB PDF posted to the API streams every clause's verdict as AG-UI events and appears in Langfuse as one trace; uploading the same file again makes zero LLM calls; a dropped connection replays without loss.

### Phase 6 · Frontend

React workspace for a lawyer's daily review: the contract as sent, with clause annotations, a synced findings panel, a risk table and feedback on every finding. Plan: `docs/Phase 6 Plan.md`.

**Goal:** a lawyer can review a whole AGB in the browser, check every finding against the statute text in one click, and say whether it was right.

**Tasks:**

- 6.A Foundation: `web/` with TypeScript, React, Vite, Tailwind + shadcn/ui, TanStack Query, i18n (German first), API and AG-UI client, `make web`
- 6.B Home and upload: list of reviews with counts and progress, drop zone for PDF/DOCX/text, scope selection, explicit error states
- 6.C Document viewer: pdf.js with a highlight layer drawn from clause coordinates, live states (queued, judging, done, failed)
- 6.D Findings panel and detail drawer: hover preview, click to pin, two-way sync with the document, statute text inline
- 6.E Table view: sort, filter, search, jump to clause
- 6.F Feedback: 👍/👎 with reason chips and corrections, developer-only per-step votes
- 6.G Quality: keyboard navigation, accessibility, dark mode, Playwright end-to-end run

**Gate:** a full AGB runs end to end in the browser, survives a page reload by reading back stored results, and a 👎 with a correction arrives in Langfuse on the right step.

### Phase 7 · Human in the loop

Turns findings into the lawyer's decisions. The graph pauses on uncertain verdicts; review cards let the lawyer accept, override or dismiss; decisions resume the run and are pushed to Langfuse as human-feedback scores. Lawyer-agreement becomes a tracked metric.

**Goal:** the result of a review is the lawyer's judgement, recorded and defensible, not the model's output.

**Tasks:**

- 7.A Finding status (Open, Accepted, Overridden, Dismissed), notes, review progress bar
- 7.B Graph interrupt on `uncertain` with a Postgres checkpointer (`langgraph-checkpoint-postgres`), resume on decision
- 7.C Accept and override recorded as feedback (one mechanism), lawyer-agreement metric per `agent_version`
- 7.D Audit trail: who decided what, when, against which `agent_version`
- 7.E Corrected verdicts promoted to a Langfuse dataset for continuous evaluation

**Gate:** an interrupted run survives a page reload and resumes correctly, and every decision is visible in the audit trail and in Langfuse.

### Phase 8 · Polish and deliverable

The work product a lawyer hands on: an export of their decisions, a summary, and help rewriting a void clause.

**Goal:** from a raw document to a DD-ready export without leaving the tool.

**Tasks:**

- 8.A Summary block: void clauses by paragraph, top risks, recommendation
- 8.B Export of the lawyer's reviewed result: DD memo (Word or Markdown) and CSV, with date, reviewer and `agent_version`
- 8.C Follow-up chat for rewrite suggestions ("Rewrite 6.1 to comply")
- 8.D Cost and latency per clause and per review in the UI
- 8.E Optional: trigger-phrase highlighting inside a clause, if continuous evaluation shows it is reliable

**Gate:** a complete DD-ready export from a raw document.

### Phase 9 · DSGVO hardening

PII masking before the LLM call and before trace storage, EU hosting confirmed for every store, AVV / Art. 28 checklist documented, retention and deletion. Required before any real client AGB enters the system; AGB-DE and public shop AGB are already non-personal, so this can trail the build.

**Tasks:** PII masking, deletion with reference counting for shared clauses, retention policy, EU object storage for files, login and per-user access, AVV checklist.

**Gate:** no unmasked text in Langfuse, and a deleted document leaves nothing behind that only it used.

### Phase 10 · AVV checker (stretch)

Art. 28(3) DSGVO clause checks against a small hand-labeled set, reusing the P1 and Phase 4 machinery with a different norm set.

### Continuous evaluation (all phases from 5)

Prompt, retrieval and threshold tuning toward void recall above 0.70 and F1 above 0.276, run as Langfuse experiments and recorded in `docs/Metrics.md`. From Phase 6 on it is fed by real feedback. It never blocks a phase. See `docs/Phase 4 Plan.md`.

## Notes on sequencing

- Phases 1–2 produce code that will run rarely; make it idempotent, then stop touching it
- Phase 3 is not optional — without a committed baseline, "the agent is better" is unfalsifiable
- Phase 4 is where the project succeeds or fails; everything from Phase 5 on presents a result already in hand
- Phase 5 (backend) and Phase 6 (frontend) are separate so the API is finished and tested before any screen depends on it
- If time runs short, cut Phase 8 scope and Phase 10 entirely; 5 → 6 → 7 → 9 is the core path

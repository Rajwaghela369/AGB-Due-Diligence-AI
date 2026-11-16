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

### Phase 5 · AG-UI streaming

FastAPI exposing the graph over AG-UI; minimal React page with the step feed and a risk table that fills in live. Paste and `.txt` input.

**Gate:** a full AGB runs end to end in the browser as one Langfuse trace.

### Phase 6 · Human in the loop

Graph interrupt on uncertain verdicts, review cards in the UI, decisions resume the run and are pushed to Langfuse as human-feedback scores. Lawyer-agreement metric becomes trackable.

**Gate:** an interrupted run survives a page reload and resumes correctly.

### Phase 7 · Polish and deliverable

Summary block, clause highlighting, CSV and Markdown export, follow-up chat for rewrite suggestions, PDF ingestion with clause splitting (a new failure surface — the prototype assumes pre-split clauses), cost and latency per clause.

**Gate:** a complete DD-ready export from a raw document.

### Phase 8 · DSGVO hardening

PII masking before the LLM call and before trace storage, EU hosting confirmed, AVV / Art. 28 checklist documented. Required before any real client AGB enters the system; AGB-DE is already anonymized, so this can trail the build.

**Gate:** no unmasked text in Langfuse.

### Phase 9 · AVV checker (stretch)

Art. 28(3) DSGVO clause checks against a small hand-labeled set, reusing the P1 and Phase 4 machinery with a different norm set.

## Notes on sequencing

- Phases 1–2 produce code that will run rarely; make it idempotent, then stop touching it
- Phase 3 is not optional — without a committed baseline, "the agent is better" is unfalsifiable
- Phase 4 is where the project succeeds or fails; everything from Phase 5 on presents a result already in hand
- If time runs short, cut Phase 7 scope and Phase 9 entirely

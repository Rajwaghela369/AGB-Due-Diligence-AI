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
- `docs/Phase 3 Results.md` - the baseline to beat, and why recall is the unstable half
- `docs/Phase 4 Plan.md` - the agent, step by step, and the three decisions it waits on

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
cp .env.example .env     # fill in ANTHROPIC_API_KEY and the Langfuse keys
make install
make smoke               # Phase 0 gate
```

## Current state

Baselines to beat, with the caveats that matter, in `docs/Metrics.md`:

| Metric | Value | Beat it in |
| --- | --- | --- |
| Void F1 (dev, 419 clauses) | **0.276** | Phase 4 |
| Retrieval primary@5 (real clauses) | **0.258** | Phase 4 |


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

`make eval` and the later targets are not implemented yet.

## Stack

Python 3.12, uv, LangGraph, FastAPI, Qdrant Cloud for vectors, Neon Postgres for
tables, Prefect Cloud for pipeline orchestration and run observability, Jina for
embeddings, Anthropic (Claude) for the router, judge and baseline models, and Langfuse (Cloud EU) for traces,
prompts, datasets and scores.

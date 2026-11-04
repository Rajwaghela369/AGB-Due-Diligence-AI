# AGB-Check

Agentic review of German consumer terms and conditions (AGB): flags potentially void
clauses, cites the BGB paragraph, and explains why. Learning prototype in the legal
due diligence area.

## Documents

- `docs/AGB-Check Agentic AGB Review for German Legal Due Diligence.md` - project brief, system design, evaluation
- `docs/Data pipelines.md` - P1 knowledge base ingestion, P2 evaluation data preparation
- `docs/Development Phases.md` - build order and the gate for each phase

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
stores/          Chroma collection and app.db (build artifacts)
reports/         quality gate output, committed for its git history
```

Boundaries are enforced by `.importlinter`, checked in CI: `app` and `pipelines` are
siblings and may not import each other, pipelines write the stores, the app reads them.

## Getting started

```bash
cp .env.example .env     # fill in OPENAI_API_KEY and the Langfuse keys
make install
make smoke               # Phase 0 gate
```

## Current state

Phase 0. `make smoke` is the gate; `make kb` and the later targets are not implemented yet.

## Stack

Python 3.12, uv, LangGraph, FastAPI, Chroma, bge-m3 embeddings (local), OpenAI for the
router and judge models, Langfuse (Cloud EU) for traces, prompts, datasets and scores.

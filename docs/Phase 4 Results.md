# Phase 4 Results

## Implementation status

The Phase 4 agentic RAG path is implemented through the evaluation harness.
Metrics are intentionally not claimed here until a live run is made against the
published Qdrant and Neon stores.

Implemented components:

- `core.retrieval`: the shared, manifest-checked Qdrant retriever used by the
  pipeline scorer and the agent; runtime retrieval policy is in `configs/app.yaml`.
- AGB-control paragraph reachability and `top_k: 10` policy from Experiment 1.
- Label-free clause loading, normalized content hashes, and verdict upserts keyed
  by `(clause_id, agent_version)`.
- Local fallback prompts plus Langfuse seeding, with a per-call legal-basis enum
  constrained to retrieved chunk IDs.
- Route → retrieve → judge → deterministic self-check → one broadened retry →
  persist graph.
- Harness-compatible task and grounding-rate evaluator.

## Retrieval experiment 1

The shared retriever first reproduced the pre-Phase-4 real-clause baseline exactly:
primary@5 **0.258**, MRR **0.142**, and 23 misses over 31 clauses. After enabling
`top_k: 10` and always including §§ 305–310, primary@5 rose to **0.355**, MRR to
**0.194**, and misses fell to 20. The report is `reports/p2_retrieval_recheck.json`.

The Qdrant manifest was migrated from the old hash to the equivalent lineage hash
after verifying that only runtime retrieval/gate configuration had changed; no vectors
were rebuilt.

## Gate and measurement

Phase 4 closes on runnability, not on a metric (see `docs/Phase 4 Plan.md`): `make
eval-smoke` completes 5 dev clauses with zero task failures, every void verdict is
grounded, each clause has a nested Langfuse trace whose id is on its `verdicts` row, and
`make check` is green. The smoke-run evidence is recorded here when the gate is signed off.

Void recall and F1 are continuous evaluation, not a gate. `make eval` runs the
stratified 70-clause dev slice (`reports/p4_agent_dev70.json`); `make eval-held-out` is
for confirming a configuration worth keeping. Targets to work towards: recall above
0.70, F1 above the Phase 3 baseline of 0.276, grounding rate 1.0.

`reports/p4_agent_dev.json` is the first, pre-fix run on Anthropic (F1 0.286 with 229
of 419 items failed: credit exhaustion and unguarded parsing). It is kept as a record of
those failures, not as a measurement.

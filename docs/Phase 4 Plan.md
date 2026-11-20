# Phase 4 Plan — Agentic RAG core

Revised Nov 16, 2026. Supersedes the Nov 15 plan, which gated Phase 4 on a 70-clause
baseline-vs-agent comparison. Phase 4 now closes when the agent is **runnable**: it takes a
clause, grounds a verdict in retrieved law, explains it, traces it, and persists it.
Measuring and improving that verdict is a continuous track (see **Continuous
evaluation**), not something later phases wait on.

## Goal

Make the verdict grounded in actual law instead of the model's recollection of it, as a
headless graph that later phases (AG-UI streaming, human in the loop) can call.

**Gate:** the agent runs end to end on real clauses —

- `make eval-smoke` completes on 5 dev clauses with **zero task failures**
- every `potentially_void` verdict is grounded (grounding rate 100%, true by construction)
- each clause produces one nested Langfuse trace, and its `verdicts` row carries that
  trace id and the `agent_version`
- `make check` is green

No F1 or recall threshold gates Phase 4. Those numbers are tracked, not gated.

## Why the scope changed

The first run (`reports/p4_agent_dev.json`, 419 items, F1 0.286) failed on 229 items:
113 from exhausted Anthropic credit, 116 from an unguarded `_parse()`. The Nov 15 plan
answered that with a stratified 70-clause comparison on Gemini, against a re-run baseline.
The baseline re-run then failed on all 70 clauses with a 404 (`gemini-2.5-flash` is
retired for new users).

Each round of measurement was costing more time than the build. The fixes those runs
pointed at are now in, and the agent has been run successfully by hand. Prompt, `top_k`
and chunk-granularity tuning will go on for the life of the project; holding Phase 5 until
they converge would stall everything behind an open-ended loop.

## Architecture: Gemini Decisions, not a generating judge

`gemini-decision-1` runs on the **Decisions API**, which answers typed questions over an
input and **generates zero output tokens**. It cannot write `reason_de`, so explanation is
a separate, conditional text call. For everything else it is a better instrument than a
generating judge:

- **Up to 1,000 questions per request, one forward pass**, shared input billed once. The
  judge is `is_void` plus one question per candidate chunk, in a single call.
- **Per-chunk probabilities** instead of one enum pick — and grounding cannot be
  hallucinated, because we supply the chunk list.
- **Calibrated confidence from the API**, so the 0.6 abstention threshold means something.
- **Context-aware decision router.** Routing is a typed Gemini Decisions question over
  the clause, not a free-form generation. Its topic is a hint, never a gate: `general`
  chunks and §§ 305–310 are always in the candidate set, so a mis-routed clause costs
  ranking, not evidence. API reviews cache the topic before the runner and the graph
  reuses it, so each new clause pays one route decision, not two.

### The graph

```
route: classify topic (Gemini Decisions choice)
  -> retrieve top_k = 10 from {topic, general} + §§ 305–310 always included
  -> ONE decide() call: {is_void: noul} + {chunk_id: noul} per candidate
  -> threshold -> verdict, legal_basis (by probability), calibrated confidence
  -> self-check: empty basis -> retry once broadened; else low confidence -> uncertain
  -> if potentially_void and explain_enabled: ONE complete() call for reason_de
  -> persist verdict keyed by (clause content hash, agent_version)
```

**Cost per new clause:** 1 Gemini Decisions route call + 1 Jina query embedding + 1
Gemini Decisions legal-judgment call + ~0.3 text calls. Cached review topics and verdicts
avoid those calls on repeats.

### Verified SDK surface

- `client.decisions.create(model=, input=, questions=)`; `NoulAnswer.noul/.confidence`,
  `ChoiceAnswer.choice/.probabilities/.confidence`; usage as
  `total_input_tokens`/`total_output_tokens`.
- Generated text uses **`response_json_schema`** with `response_schema` omitted (sending
  both is rejected; a test pins it). Tokens from `usage_metadata.prompt_token_count` /
  `.candidates_token_count`.
- `noul` is clamped 0.01–0.99, so no threshold above 0.99 can fire.

### One deliberate conflict, resolved

"Never raise on a provider error" and "a half-finished run should stop deliberately"
conflict for budget exhaustion. `BudgetExceeded` **propagates**; genuine provider faults
are absorbed into an error-carrying result. Swallowing it would turn quota exhaustion into
hundreds of verdict-shaped errors — how the $5 Anthropic run degraded instead of stopping.

## Tasks

### 4.A · Required fixes — done

| Fix | Where |
| --- | --- |
| `_parse` never raises; a bad response yields a null verdict, not an exception | `graph.py` |
| Cap `reason_de` length, raise `max_output_tokens` | `prompts.py` |
| Nothing reads the annotators' `topics` | `graph.py` |
| Real `agent_version` from config, index hash and prompt fingerprints | `task.py` |
| Self-check order: retry on empty basis first, then abstain on low confidence | `graph.py` |
| Rich run metadata (models, prompt fingerprints, `agent_version`, retrieval policy) | `run_agent.py` |
| Tests for retrieval, hashing, prompts, verdicts, clauses — fakes only, no network | `tests/` |

### 4.B · Tracing — done

One `agent` observation per clause, with `retriever` spans (ranked chunk ids, citations,
scores), `generation` spans (model, prompt version, tokens), and `span`s for self-check
and retry. The trace id is written to the `VerdictRecord`; traces are tagged with
`agent_version` and split; nesting works with `tracer=None`.

### 4.C · Provider abstraction and request budget — done

`core.llm` exposes `decide()` and `complete()` behind `model["provider"]` (Gemini;
Anthropic kept reachable for a possible stronger judge later). `core.budget` counts
requests per run and raises *before* the call that would exceed `run.request_budget`;
`max_concurrency` is 4 with an RPM cap and backoff on 429.

### 4.D · Retrieval policy — done

`top_k: 10`; §§ 305–310 always reachable; the topic filter stays, fed by the Gemini
decision router. The AVV obligation router remains a separate multi-label embedding
router because AVV clauses can satisfy several Art. 28 obligations at once.
Experiment 1 on real clause text: primary@5 **0.258 → 0.355**, MRR 0.142 → 0.194
(`reports/p2_retrieval_recheck.json`). Removing the topic filter entirely was measured
too and scored 0.258, below 0.355, so the router was kept.

### 4.E · Make it runnable — remaining

**Goal:** anyone can run the agent from a clean checkout, and the repo says so.

- Drop the baseline from the v1 workflow: the `baseline` Makefile target's help text and
  the `eval` target's "same slice as `make baseline`" wording; the comparison framing in
  `configs/evals.yaml`. The Phase 3 baseline code and reports stay as history.
- Bring stale descriptions in line with the code: the `graph.py` docstring still says
  "there is no router node", and `.env.example` and the README still describe Anthropic as
  the router and judge provider.
- `make eval-smoke` as the documented entry point; README section on running the agent
  (env vars, `make seed-prompts`, `make eval-smoke`, where the trace and verdict land).
- Commit the Phase 4 work, which is currently uncommitted on `phase-2-eval-prep`.

**Done when:** the gate above holds from a clean checkout.

### 4.F · Record

`docs/Phase 4 Results.md`: what was built, the retrieval policy and its numbers, the
smoke-run evidence for the gate, and the known limitations. It states plainly that Phase 4
closed on runnability, and that F1 and recall are tracked in `docs/Metrics.md`, not
claimed as a gate.

## Continuous evaluation

Runs alongside Phase 5 onwards, never blocking it. Each run is a Langfuse experiment run
under `agb-void-classification`, reconstructable from its `agent_version` and prompt
fingerprints, and recorded in `docs/Metrics.md` with its slice stated.

- `make eval` — the stratified dev slice (31 void + 39 non-void, seed 0), reporting raw
  and **weighted** precision (×9.95 per non-void false positive). Tune on weighted
  precision; raw precision on a 44%-void slice rewards over-flagging.
- Targets to work towards: void recall > 0.70, F1 clearly above Phase 3's 0.276,
  grounding 100%, task failures < 2%.
- `make eval-held-out` — once, when a configuration is worth confirming, not per iteration.
- Improvement candidates, roughly in cost order: decision thresholds, prompt wording,
  hybrid/BM25 for the register mismatch (Phase 2 cause 2), chunk granularity, a stronger
  judge via Anthropic, and the three gold mappings awaiting legal review.

## Open decisions

1. **Free-tier quota.** `request_budget: 10000` and `rpm: 180` are taken from the published
   Gemini limits; confirm them against this key before a full 419-clause run.

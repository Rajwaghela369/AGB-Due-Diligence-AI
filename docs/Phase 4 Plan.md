# Phase 4 Plan — Agentic RAG core

Nov 13, 2026

## Goal

Make the verdict grounded in actual law instead of the model's recollection of it.

The Phase 3 baseline guessed: 121 void calls on 419 dev clauses to find 21, citing
§§ 307–309 fluently without ever reading them. Phase 4 replaces the guess with a
pipeline — topic router → retrieve the real BGB text → judge the clause against it →
self-check → retry retrieval once, or finish — so every verdict carries the paragraph it
rests on.

**Gate** (`docs/Development Phases.md`): void recall > 0.70, F1 > **0.276** on dev,
grounding rate 100%. Clauses the judge cannot decide are marked `uncertain`, not guessed.

**Known obstacle:** retrieval fetches the right paragraph 26% of the time
(`docs/Phase 2 Results.md`). Grounding a judge in the wrong § fixes nothing, so steps
4.1–4.2 fix retrieval before any agent code exists.

**Structural constraint:** `app/` may not import `pipelines/` (enforced by
`.importlinter`). Retrieval currently lives in `pipelines/p1_kb/s5_check_retrieval.py`,
so the agent cannot reuse it. Step 4.1 exists because of this.

## Steps

### 4.1 · One retriever, shared by the agent and the scorer

**Goal:** make the 0.258 re-check measure the agent's *actual* retrieval, not a lookalike.

**Build:** `packages/core/retrieval.py`

```python
@dataclass(frozen=True)
class Retrieved:
    chunk_id: str; citation: str; text: str; score: float; rank: int

def retrieve(text: str, topic: str, policy: dict, depth: int = 20) -> list[Retrieved]
```

Embeds via `core.embeddings.embed_texts` (Jina, `retrieval.query` task), queries the alias
`bgb_agb`, asserts the `StoreManifest` against the config hash on first use.

**Technical detail:**

- Retrieval *policy* — `top_k`, `always_include_topics`, `always_include_paragraphs`,
  `search_depth` — moves from `configs/kb.yaml` to `configs/app.yaml`. `kb.yaml` is hashed
  into the index lineage, so today a `top_k` change would make the app refuse its own
  index. Policy is runtime, not lineage.
- `pipelines/p1_kb/s5_check_retrieval.py` is refactored to call `core.retrieval.retrieve`
  and keep only the scoring.
- Tests: filter construction against a fake Qdrant client, with the `other`-tag case
  covered explicitly.

**Done when:** `make recheck` reproduces primary@5 **0.258 exactly** on the refactored
path. Behaviour-preserving first, so the next step's change is attributable to the change.

### 4.2 · Experiment 1: fix reachability, then widen the candidate set

**Goal:** stop 7 of 31 answers being unreachable by construction. This is the experiment
already specified in `docs/Development Phases.md`; baseline 0.258 / 0.333-reachable.

**Build:** two config changes in `app.yaml`, measured separately.

1. `always_include_paragraphs: [305, 305a, 305b, 305c, 306, 306a, 307, 308, 309, 310]`.
   The filter becomes `kind = chunk AND (topic ∈ {routed, general} OR paragraph ∈
   AGB-control)`. Uses the existing `paragraph` payload index — **no index rebuild**,
   lineage untouched. Chunks tagged `other` stop being invisible.
2. `top_k: 10` for the agent's candidate set. Phase 2 measured reachable @10 at 0.833: the
   right chunk is usually *found*, just ranked 6–13. Ten candidates let Sonnet discriminate
   what the embedder cannot rank.

**Decision point:** if primary@10 over all 31 clears ~0.75, hybrid/BM25 (cause 2, the
register mismatch) is deferred. If not, it becomes 4.2b.

**Done when:** `make recheck` shows the 7 formerly-unreachable queries with a rank, and
`docs/Metrics.md` gains the new row.

### 4.3 · Clause loader and verdict store

**Goal:** the agent reads clauses the only way it is allowed to, and writes verdicts
somewhere permanent.

**Build:** `app/agent/clauses.py`, `app/agent/verdicts.py`

**Technical detail:**

- `load_clauses(split=None) -> list[Clause]` reads Neon `clauses` via `core.db` and calls
  `assert_no_labels(columns, "clauses table at read")` — the third assertion of the
  invariant that `docs/Phase 2 Results.md` promises. Now actually true.
- `clauses` gains a `content_hash` column: `sha256` of the clause text after whitespace
  collapse and Unicode NFC, and nothing more. The 523 AGB-DE rows keep `agb-NNN` as their
  stable external id; the hash is the dedup key the storage design below builds on. Writing
  it also tells us how many exact-duplicate clauses the corpus holds (Phase 2 found 11
  across the split; `agb-2808` and `agb-3123` are one example).
- `verdicts` table keyed **`(clause_id, agent_version)`**, with `run_id` as a plain column.
  Other columns match `core.schemas.VerdictRecord`: `verdict`, `legal_basis text[]`,
  `reason_de`, `confidence`, `lawyer_decision` (null until Phase 6), `langfuse_trace_id`.
  Upsert on the key, so the same agent version judging the same clause converges, and a
  new version writes beside the old rather than over it — verdicts are superseded, never
  overwritten.
- `agent_version` is `core.config.config_hash` over everything that can change a verdict:
  both prompt versions, both model ids, the retrieval policy block, and the index's own
  `config_hash`. Same pattern the index already uses, one level up.
- Tests with the fake-connection pattern from `tests/test_publish.py`.

**Done when:** the loader returns 523 `Clause` objects with no label field reachable; a
`VerdictRecord` round-trips; two writes with the same `(clause_id, agent_version)` leave
one row.

**Why this key, and what it buys later.** Keying verdicts on content and agent version
makes the table a cache: a clause this version of the agent has already judged is served
from Postgres with no LLM call. The 523 eval clauses are the first beneficiaries — rerunning
an unchanged agent costs nothing — but the real payoff is the upload path in Phase 5, where
the same template keeps coming back with small edits. The storage design that completes
this is recorded below so it is decided once.

### 4.4 · Prompts in Langfuse, grounding enforced by schema

**Goal:** prompts versioned outside the code, and a judge that *cannot* cite a paragraph it
was not shown.

**Build:** `app/agent/prompts.py`, `scripts/seed_prompts.py`

**Technical detail:**

- Two prompts in Langfuse prompt management, `agb-router` and `agb-judge`, fetched by
  name + label (`production`), cached per process. **Local fallback files** in
  `app/agent/prompts/` so unit tests run offline and CI needs no key.
- Judge output via `output_config.json_schema`, as in the baseline — with one addition
  that does real work:

  ```python
  "legal_basis": {"type": "array", "items": {"enum": [c.chunk_id for c in candidates]}}
  ```

  The enum is built **per call from the retrieved set**. The API rejects any citation not
  in it. Grounding is not checked afterwards; it is impossible to violate. Plus `verdict`,
  `reason_de`, `confidence`.
- Router output: `{"topic": "liability" | "warranty" | "other"}`.

**Done when:** `seed_prompts.py` has pushed v1 of both; a fake-client test proves an
out-of-set `chunk_id` fails the schema.

### 4.5 · The graph

**Goal:** the headless agent — route → retrieve → judge → self-check → retry once or
finish — one Langfuse trace per clause.

**Build:** `app/agent/graph.py` (LangGraph `StateGraph`), `app/agent/state.py`

```
State: clause, topic, candidates, draft (verdict / legal_basis / reason / confidence),
       retries, trace_id

route        Haiku 4.5. Reads ONLY clause text + title (see open decision 1).
retrieve     core.retrieval.retrieve(text, topic, policy)
judge        Sonnet 5, schema-constrained to candidates.
self_check   deterministic, no LLM:
               confidence < 0.6              → verdict = uncertain
               potentially_void, empty basis → retry   (if retries < 1)
               else                          → finish
retry        broaden: topic → general-only + AGB-control, depth ×2, re-judge.
finish       persist VerdictRecord, return.
```

- Conditional edges off `self_check`. `max_retrieval_retries: 1` and
  `confidence_uncertain_below: 0.6` already sit in `app.yaml`.
- Tracing via `langfuse.observe` on each node; the trace id is written into the
  `VerdictRecord`.
- Tests run the graph with fake router / judge / retriever to cover every edge: happy path,
  low confidence → uncertain, empty basis → retry → finish, retry exhausted.

**Done when:** `make eval --limit 5` produces 5 traces in Langfuse, each with the four
nodes visible and a persisted verdict.

### 4.6 · Plug into the harness, add the grounding gate

**Goal:** the agent scored by exactly the code that scored the baseline.

**Build:** `app/agent/task.py`, `evals/run_agent.py`, one new evaluator

**Technical detail:**

- `task.run(*, item, **_) -> {verdict, reason_de, confidence, legal_basis, error}` —
  identical signature to `evals.baseline.task`. The harness, scorers, datasets and config
  stay **untouched**; that was the point of building them agent-agnostic.
- New item evaluator `grounded`: a `potentially_void` verdict with ≥ 1 `legal_basis` id
  that was in the candidate set. New run evaluator `grounding_rate`. **The gate is 100%**,
  and with the schema enum from 4.4 it should be 100% by construction — so the evaluator
  is the proof, not the mechanism.
- Same Langfuse experiment name `agb-void-classification`, so agent runs sit beside
  `baseline-dev-20261111-1701` in the UI.
- `make eval` (dev) and `make eval-held-out`. The `evals.yml` smoke switches to the agent
  and keeps the 10% task-failure guard.

**Done when:** a full dev run writes `reports/p4_agent_dev.json` with F1, grounding rate,
and false negatives / positives by clause id.

### 4.7 · Iterate, on dev only

**Goal:** beat F1 0.276 honestly.

Two or three loops, each a Langfuse run compared on dev's 31 positives: judge prompt
wording, `top_k` 10 vs 15, whether the retry helps or only spends. Held-out **once**, at
the end, read with the ±0.125 rule from `docs/Metrics.md`.

**Done when:** dev F1 > 0.276, dev recall > 0.70, grounding 100% — or a documented reason
it is not.

### 4.8 · Record

`docs/Phase 4 Results.md`, the `docs/Metrics.md` scoreboard, the gate ticked in
`docs/Development Phases.md`, and the error analysis: which void clauses still slip
through, and whether they are the same three the baseline missed and Phase 2 had already
doubted (`agb-1464`, `agb-1607`, `agb-3490`).

## Storage design for the upload path (decided here, built in Phase 5)

The question that prompted this: if someone uploads the same contract again and again, the
naive design stores it again and again, and judges it again and again. The answer is
**content-addressed storage** — identify documents and clauses by a hash of their text,
not by when they arrived. It is how git stores files, and it fits this problem exactly.

```
uploads            who uploaded what, when            one row per upload EVENT
   │ many → one
documents          document_id = sha256(normalised full text)
   │ one → many
document_clauses   (document_id, position, clause_id)  the order within a document
   │ many → one
clauses            clause_id = sha256(normalised clause text)   ← content_hash from 4.3
   │ one → many
verdicts           (clause_id, agent_version) → verdict, legal_basis, reason, …
```

Four things fall out of hashing, none of which needs extra code:

1. **Re-uploading an identical contract stores nothing new.** Same text, same
   `document_id`, document already present. A new `uploads` row keeps the audit trail
   (who, when); zero new clause rows; zero LLM calls.
2. **Identical clauses across shops are stored once.** `agb-2808` and `agb-3123` are
   word-for-word the same clause in two contracts; they become one `clauses` row with two
   `document_clauses` references.
3. **Verdicts are a cache.** A clause this `agent_version` has already judged is answered
   from the table — instant and free. A re-uploaded contract gets its whole risk table back
   in milliseconds.
4. **An edited contract re-judges only what changed.** Fifty unchanged clauses hash to
   existing ids and reuse their verdicts; the three reworded ones are judged. In due
   diligence, where the same template returns with small edits, this is the real saving.

**Normalisation — the one place to be careful.** Hash after collapsing whitespace and
applying Unicode NFC, and nothing else. Over-normalise and clauses that differ in a word
that matters merge — "nur bei Vorsatz" and "nur bei Vorsatz und grober Fahrlässigkeit" are
different clauses with different verdicts. Under-normalise and a stray double space defeats
the dedup.

**Sequencing.** 4.3 adds `content_hash` to `clauses` and keys `verdicts` on
`(clause_id, agent_version)`, so the two tables that exist today are already in the right
shape. `uploads`, `documents` and `document_clauses` wait for Phase 5, when something can
actually write to them — building tables with no writer is how schemas drift. Phase 7's PDF
splitter writes into the same tables; nothing here changes for it.

## Open decisions — to settle before 4.1

1. **The router must not read `input.topics`.** The eval items carry `topics` from
   AGB-DE's *annotators*. A real client's AGB arrives untagged, so a router that reads them
   would score well on the test and fail in use. Proposal: the router reads clause text
   only, and the annotators' topics become **free ground truth for router accuracy** — a
   diagnostic for nothing. The 8 multi-topic clauses are where it will legitimately
   disagree.

2. **Retrieval policy moves out of `kb.yaml` lineage into `app.yaml`.** Otherwise every
   `top_k` tweak changes the index's config hash and the app refuses to start. One-time
   move in 4.1.

3. **Self-check stays deterministic in this phase.** Confidence threshold, empty-basis
   retry, nothing else. An LLM critic is a plausible 4.7 experiment, but it adds cost
   before it adds a measurable number. Start without it.

## Files this phase touches

```
packages/core/retrieval.py         new   4.1
configs/app.yaml                   edit  4.1, 4.2  (retrieval policy block)
configs/kb.yaml                    edit  4.1       (retrieval block removed)
pipelines/p1_kb/s5_check_retrieval.py  edit  4.1  (calls core.retrieval)
app/agent/clauses.py               new   4.3       (+ content_hash on the clauses table)
app/agent/verdicts.py              new   4.3       (keyed clause_id + agent_version)
app/agent/prompts.py               new   4.4
app/agent/prompts/*.md             new   4.4       (offline fallbacks)
scripts/seed_prompts.py            new   4.4
app/agent/state.py                 new   4.5
app/agent/graph.py                 new   4.5
app/agent/task.py                  new   4.6
evals/run_agent.py                 new   4.6
evals/harness.py                   edit  4.6       (grounded evaluator)
.github/workflows/evals.yml        edit  4.6
Makefile                           edit  4.6       (eval, eval-held-out)
docs/Phase 4 Results.md            new   4.8
docs/Metrics.md                    edit  4.2, 4.8
```

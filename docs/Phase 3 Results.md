# Phase 3 Results

Baseline and eval harness. A one-shot classifier with no retrieval, scored on the
void class, as a Langfuse experiment.

**Status:** complete. Baseline recorded on both halves, harness permanent in `evals/`,
CI job in `.github/workflows/evals.yml`.

## The numbers

| | clauses | void | precision | recall | **F1** | void calls |
| --- | --- | --- | --- | --- | --- | --- |
| dev (`train`) | 419 | 31 | 0.174 | 0.677 | **0.276** | 121 (29%) |
| held-out (`test`) | 104 | 8 | 0.184 | 0.875 | **0.304** | 38 (37%) |

Model `claude-haiku-4-5-20251001`, no BGB context, one call per clause. **Zero task
failures and zero abstentions** across all 523 calls — the JSON schema is enforced by
the API, so `parse_answer`'s fallbacks never fired.

**Phase 4 must beat dev F1 0.276.** Held-out F1 0.304 is the gate number recorded in
Langfuse; it is not the comparison target, for the reason below.

## What the baseline does wrong

It over-calls void: 121 void calls on dev to find 21 real ones, **100 false alarms**,
roughly five wrong for every one right. Its reasons read plausibly and cite §§ 307, 308
and 309 fluently — while having never seen their text:

> `agb-115`: "Die Klausel versucht, Haftung für Datenverlust und Übertragungsfehler
> pauschal auszuschließen. Dies verstößt gegen § 307 …"

That is the predicted failure of an ungrounded model: it pattern-matches "sounds
restrictive" onto "void". **Precision, not recall, is the baseline's weakness, and so
precision is what Phase 4 has to earn.** Retrieval is the right instrument for it — the
judge can check whether a prohibition actually applies instead of guessing.

## Precision is stable, recall is not

Precision came out 0.174 and 0.184 on the two halves, which agree because their base
rates do (7.4% and 7.7%). Recall came out **0.677 on dev and 0.875 on held-out** — the
same classifier, a 0.2 spread.

That is the 8-positive instrument problem, demonstrated rather than argued. Held-out
recall of 0.875 is 7 of 8 clauses; one clause either way is ±0.125. Dev's 31 positives
give a reading about twice as stable.

It also corrects an expectation recorded before the runs: precision was predicted to
fall with the base rate, which it did, but recall was predicted to hold, and it did not.
Recall is not base-rate dependent in theory; it is sample-size dependent in practice.

**Consequence for Phase 4:** compare prompts and retrieval settings on dev. Use
held-out once per phase, and read a change under ±0.125 as noise.

## The clauses it missed, and what three of them say about our answer key

Dev false negatives: `agb-441`, `agb-456`, `agb-458`, `agb-1198`, `agb-1464`,
`agb-1607`, `agb-2794`, `agb-2808`, `agb-3123`, `agb-3490`.

Three of those ten are clauses whose *void label we had already questioned*, independently,
when building the retrieval answer key in Phase 2:

- **`agb-1464` and `agb-1607`** — set-off clauses marked `confidence: low` in
  `configs/gold_map.yaml` with the note that, read literally, they permit set-off with
  undisputed or finally adjudicated claims, which is exactly what § 309 Nr. 3 protects,
  so on their face they comply. The model also judged them valid.
- **`agb-3490`** — the omnibus reservation clause, likewise flagged low confidence
  because § 308 Nr. 4 and § 434 Abs. 2 are both defensible readings.

This is not evidence that the annotators are wrong. It is evidence that these particular
labels are harder than the rest, and it strengthens the case for the lawyer review those
three mappings are already waiting on. If a reviewer finds the model right and the label
questionable, the recall figure improves for a reason that has nothing to do with the
agent.

**`agb-2808` and `agb-3123` are word-for-word identical** clauses in different shops, and
both were missed. Consistent behaviour on duplicate input is a small piece of evidence
that the run is deterministic rather than noisy.

Held-out missed exactly one clause, `agb-2632`.

## What the harness is, beyond this number

The baseline is throwaway. `evals/` is not:

- `scorers.py` — void-class precision/recall/F1, never averaged across classes. A model
  answering "valid" to everything is 92.5% accurate on this data and scores 0.0 here,
  asserted by test.
- `harness.py` — takes a task function and knows nothing about how a verdict was reached.
  Phase 4's graph plugs in as a different function; scorers, datasets, config and CI stay
  untouched. That is what makes the two numbers comparable.
- `datasets.py` — dev off disk, held-out from Langfuse, shaped into one identical item so
  a task function cannot tell which half it is seeing.
- A failed call becomes a null verdict scored as "not void", so a failure costs recall
  rather than shrinking the denominator. This kept the first broken smoke run
  interpretable while every call was failing.

## A note on `void_correct`

The per-item `void_correct` score in Langfuse is an accuracy-shaped metric and should not
be read as quality. During the two broken smoke runs, where every API call failed, it
averaged 0.78 — because a null verdict is "not void" and most clauses are not void. It is
useful for finding individual clauses in the UI, not for judging a run. The gate reads
void-class F1.

Two failed runs from debugging remain in the `agb-void-classification` experiment and
pollute pooled per-item averages. They carry `task_failed` 1.00 and no F1, so they are
easy to identify, and worth deleting.

## Gate

- Baseline F1 recorded in Langfuse: **dev 0.276, held-out 0.304** ✅
- CI job rerunning the suite on changes to prompts or the agent:
  `.github/workflows/evals.yml`, path-filtered, pure tests always plus a 40-clause live
  smoke when credentials exist ✅

Phase 3 closed.

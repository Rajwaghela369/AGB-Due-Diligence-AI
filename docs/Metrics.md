# Metrics

Every number this project is judged on, where it came from, and how to reproduce it.
Read this before quoting a figure: two of them look much better than they are.

Last updated after the Phase 4 retrieval experiment.

## The scoreboard

| Metric | Value | Phase | Reproduce | Report |
| --- | --- | --- | --- | --- |
| **Void F1, dev** | **0.276** | 3 | `make baseline` | `reports/p3_baseline_dev.json` |
| Void F1, held-out | 0.304 | 3 | `make baseline-held-out` | `reports/p3_baseline_held_out.json` |
| **Retrieval primary@5, real clauses** | **0.258** | 2 | `make recheck` | `reports/p2_retrieval_recheck.json` |
| Retrieval primary@5, real clauses, Phase 4 policy | **0.355** | 4 | `make recheck` | `reports/p2_retrieval_recheck.json` |
| Retrieval primary@5, synthetic | 0.933 | 1 | `make kb` | `reports/p1_retrieval_check.json` |
| Clause counts | 523 / 39 void | 2 | `make eval-data` | `reports/p2_counts.json` |

The two bold rows are the live baselines. **Phase 4 must beat void F1 0.276 and should
move primary@5 above 0.258.**

## Void classification (Phase 3 baseline)

One Haiku 4.5 call per clause, no retrieval. The thing Phase 4 exists to beat.

| | clauses | void | precision | recall | **F1** | void calls |
| --- | --- | --- | --- | --- | --- | --- |
| dev (`train`) | 419 | 31 | 0.174 | 0.677 | **0.276** | 121 (29%) |
| held-out (`test`) | 104 | 8 | 0.184 | 0.875 | **0.304** | 38 (37%) |

Zero task failures, zero abstentions, 523 calls.

**Compare on dev, not held-out.** Precision agrees across the halves (0.174 / 0.184)
because their base rates do (7.4% / 7.7%). Recall does not: 0.677 against 0.875 for the
same classifier, because held-out has only 8 positives. One clause either way is
±0.125, so **read any held-out change under 0.125 as noise.**

**Never read `void_correct` as quality.** It is accuracy-shaped: during two broken smoke
runs where every API call failed, it averaged 0.78, because a null verdict counts as
"not void" and most clauses are not void. The gate reads void-class F1.

**Scoring rules** (`evals/scorers.py`, `configs/evals.yaml`):

- Precision, recall and F1 on the **void class only**, never averaged across classes. A
  model answering "valid" to everything is 92.5% accurate here and scores F1 0.0.
- An `uncertain` verdict is **not** a void call, so abstaining costs recall
  (`uncertain_counts_as_void: false`). Phase 7 routes abstentions to a human; until then
  they are not free.
- A failed or unparseable call becomes a null verdict scored as "not void", so a failure
  costs recall rather than shrinking the denominator.

## Retrieval

Does the index return the BGB paragraph a reviewer should cite?

| | queries | @1 | @3 | **@5** | @10 | @20 | MRR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Phase 1, synthetic | 30 | 0.433 | 0.767 | **0.933** | — | — | 0.629 |
| Phase 2, real clauses | 31 | 0.000 | 0.129 | **0.258** | 0.645 | 0.710 | 0.142 |
| Phase 2, reachable only | 24 | 0.000 | 0.167 | **0.333** | 0.833 | 0.917 | — |

**0.933 is not a usable figure.** Those 30 queries were written by Claude, and Claude
also chose their answers, with knowledge of what the index contained. It shows the index
is structurally sound, not that it works on real input — `data/gold/README.md` said so
before the re-check confirmed it.

**0.355 is the current Phase 4 retrieval number**, over 31 real void clauses whose void label comes from
AGB-DE's annotators. Its one weak link is that the *paragraph* for each was chosen by
Claude; eight of the 31 mappings are flagged `confidence: low` in `configs/gold_map.yaml`
and three are awaiting legal review.

The gap decomposes into two causes (full analysis in `docs/Phase 2 Results.md`):

1. **7 of 31 queries could never succeed under the old policy.** The topic filter admits only
   `{query topic, general}`, so chunks tagged `other` — § 308 Nr. 4, § 309 Nr. 3 — are
   unreachable by *any* query, and a liability clause whose basis is a warranty-tagged
   chunk is cut. This is a live defect in the retrieval path the agent will use.
2. **14 reachable misses all landed at rank 6–13**, 11 of them retrieving a substantive
   norm where a § 308/309 prohibition was the answer. Dense similarity finds the norm a
   clause is *about*, not the one that voids it.

Note @10 reachable is 0.833: the right chunk is usually **found**, just ranked below the
substantive norms. That is a ranking and top-k problem, not an embedding failure.

Phase 4's first retrieval experiment sets `top_k: 10` and always includes §§ 305–310
(including lettered sections). It raises primary@5 to **0.355**, reduces misses to 20,
and makes the previously unreachable control paragraphs eligible for retrieval. The
remaining rank-6–19 misses are the next retrieval/judge iteration target.

## Data counts (Phase 2 gate)

All six asserted by the flow against `configs/eval.yaml`; a mismatch fails the run before
anything is published.

| | clauses | void |
| --- | --- | --- |
| corpus (pinned commit `9b847807`) | 3764 | 179 |
| in scope (liability ∨ warranty) | **523** | **39** |
| dev (`train`) | 419 | 31 |
| held-out (`test`) | 104 | 8 |

89 contracts. 1 fragment and 18 foreign-law clauses are **flagged, not dropped**.

Known properties of the inherited answer key, each with consequences for how a number
reads — see `docs/Phase 2 Results.md`:

- Five void clauses are missing from the authors' split, all five deliberately.
- The split is clause-level: 54 of 59 held-out shops also appear in dev. Measured text
  leakage is shallow and touches **none** of the 8 held-out void clauses, so the headline
  metric is clean — but it measures generalisation to new clauses from familiar shops,
  not to a new client's AGB.

## Targets for the phases ahead

| Phase | Gate |
| --- | --- |
| 4 · Agentic RAG | void recall > 0.70, F1 > **0.276** (dev), grounding rate 100% |
| 5 · AG-UI | a full AGB end to end in the browser as one Langfuse trace |
| 6 · Human in the loop | an interrupted run survives a page reload and resumes |
| 7 · Polish | a complete DD-ready export from a raw document |
| 8 · DSGVO | no unmasked text in Langfuse |

## Reproducing anything here

```bash
make kb                 # Phase 1: build the index, score the synthetic gold set
make eval-data          # Phase 2: counts gate, publish clauses + answer key
make recheck            # Phase 2: re-score retrieval on real clause text
make baseline           # Phase 3: dev baseline, the comparison number
make baseline-held-out  # Phase 3: held-out baseline, the gate number
```

`make eval-data` and `make baseline*` call paid APIs and write to Neon, Langfuse and
Qdrant. `make eval-data-dry` asserts every gate and the label-leak invariant without
writing to any store.

Every report in `reports/` is committed for its git history, so a number can always be
traced to the run that produced it.

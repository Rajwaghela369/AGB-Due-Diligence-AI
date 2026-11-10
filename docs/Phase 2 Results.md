# Phase 2 Results

P2 EVAL-PREP: AGB-DE turned into input clauses for the agent and a separate answer key.
Numbers below are from the pinned corpus commit `9b847807`.

**Status:** steps 2.1–2.5 implemented and gated. The retrieval re-check (2.6) is not yet
built — see [Open: the retrieval answer key](#open-the-retrieval-answer-key).

## Counts

| | clauses | void | void rate |
| --- | --- | --- | --- |
| corpus | 3764 | 179 | 4.8% |
| in scope (liability ∨ warranty) | **523** | **39** | 7.5% |
| dev half (`train`) | 419 | 31 | 7.4% |
| held-out half (`test`) | 104 | 8 | 7.7% |

89 contracts in scope. All six counts are asserted by the flow against `configs/eval.yaml`
and written to `reports/p2_counts.json`.

## Decisions that depart from the design doc

### Nothing in scope is dropped

`docs/Data pipelines.md` specified dropping fragments and shops referring to foreign law.
Both exclusions were replaced with flags (`is_fragment`, `foreign_law_choice`) carried on
the row, following how P1 keeps its manifest inside the Qdrant collection and uses `kind`
to hold it out of searches. A deleted row cannot be audited later; a flagged one can.

The exclusion now happens at one boundary — the Langfuse dataset build in 2.5 — rather
than being baked into the data.

### The foreign-law filter was wrong, not just narrow

Two in-scope shops choose non-German law: contract 18 (French) and contract 29 (Italian),
together holding 18 clauses of which **3 are void**.

Under Art. 6(2) Rome I a choice of law in a consumer contract cannot deprive the consumer
of the mandatory protections of their habitual residence, so § 305 ff. BGB still governs
these clauses for a German consumer. Both shops concede the point in their own text:
contract 18 cites `EG 593/2008` directly, and contract 29's clause 14.2 reserves the more
favourable mandatory provisions of the user's home country. The annotators were therefore
right to mark those clauses void, and dropping them would have deleted correct ground
truth.

Read literally — dropping every shop that merely carries an `applicableLaw` clause — the
filter would have removed **61 of 89 contracts, 404 of 523 clauses, and 21 of 39 void
clauses**. The detector now reads the clause text for an explicit non-German choice and
matches exactly two shops.

This reasoning is Art. 6(2) Rome I applied by a non-lawyer. It changes the answer key and
is the one Phase 2 decision worth having confirmed by a lawyer.

### The fragment

One in-scope clause is under 40 characters: `agb-1362`, `"12.3 WANN SIE ANWENDBAR SIND"`,
a heading rather than a clause, not void, in the dev half. It is published to the clause
table (Phase 7's PDF splitter will produce these by the dozen, so one real example is
worth keeping) and excluded from nothing, since it is not in the held-out half that the
reported metric uses.

## Properties of the inherited answer key

### Five void clauses are missing from the authors' split

The corpus has 179 void clauses; the published train/test split covers 174. The five
unassigned rows — ids 510, 826, 1170, 1271, 1468 — are **all void**, which at a 4.8% base
rate is not chance. The AGB-DE authors held them out deliberately. None is liability or
warranty, so the scoped slice is unaffected, but the gap is theirs, not a bug in our join.

### The split is clause-level, not shop-level

54 of the 59 held-out contracts (92%) also appear in the dev half; only 5 shops are
exclusive to held-out, holding 6 clauses and 1 void clause between them.

Measured text leakage is nevertheless shallow: 13 held-out clauses share text verbatim
with a dev clause and 3 more are ≥0.90 similar, but **none of them is void**. The 8 void
clauses that determine recall and F1 are textually distinct from everything in the dev
half, so the headline metric is not contaminated.

The caveat that remains is about what the number means. This split measures generalisation
to *new clauses from mostly-familiar shops*, not to *a new client's AGB*. A shop-disjoint
split cannot be built from the authors' assignment (5 shops, 1 void). If a true new-shop
number is wanted later, it needs an own contract-disjoint split across all 89 contracts,
which costs comparability with the paper and so belongs as a clearly-labelled secondary
metric — a Phase 3 decision, once a baseline exists to compare against.

### The held-out half is a coarse instrument

8 void clauses. Catching 6 versus 7 of them is 0.75 versus 0.875 recall, and one clause
either way is noise. One of the 8 comes from the Italian shop flagged above. Comparisons
between prompt variants belong on the dev half (31 void); the held-out half is for a final
one-shot confirmation.

### `cited_refs` is diagnostic only

Only 55 of the 523 in-scope clauses cite any `§`. Of those citations, 42 name a law and 39
do not — and 23 of the unnamed ones are numbers like `§ 9`, `§ 10`, `§ 12`, which are the
shop citing its own section rather than a statute. Magnitude cannot separate them, because
`§ 13 BGB` and `§ 14 BGB` are genuine; some clauses cite the HGB. The field therefore
records the law only where it is stated and leaves it `None` otherwise. It must never be
used as retrieval ground truth.

### Multi-topic clauses

8 in-scope clauses carry two topics (`liability, warranty` ×2, plus `delivery`,
`payment`, `retentionOfTitle` and `intellectualProperty` combinations). When the Phase 4
router picks one topic per clause, these are the cases where the correct route is
ambiguous. Not a Phase 2 problem, but it bounds how far router accuracy can be read.

## Label-leak invariant

Asserted three times for one property:

1. on the frame bound for Postgres, before the insert;
2. against `information_schema.columns` after the insert, i.e. the table as the database
   reports it rather than as we built it;
3. by the app's clause loader at read time (`core.invariants`, shared by both sides).

The clause table holds `clause_id, contract_id, split, title, text, topics` and no label
column by construction. `label_void` reaches only the Langfuse dataset, in
`expected_output`.

## Open: the retrieval answer key

Phase 1's gate scored primary@5 0.933 against 30 synthetic queries written by Claude.
`data/gold/README.md` commits to re-running that check on real clause text with labels
from the dataset's authors, and names it the number to trust.

The design doc's mechanism for this — a `subtopic -> chunk_id` mapping — cannot carry that
weight. Only **16 of 523** in-scope clauses have a `subtopics` value, 14 of them in the dev
half, across 5 distinct subtopics, and `warranty:period` alone accounts for 8 of the 14 and
points at a single chunk. That yields roughly five effective queries.

The proposed alternative is to hand-map the **31 void dev-half clauses** to chunk ids: the
`void` annotation is the authors' expert signal, the index holds only 105 chunks, and a
void clause is void under a determinate norm. This keeps the held-out half sealed and
produces a real-text answer key of roughly Phase 1's size. Not yet built or agreed.

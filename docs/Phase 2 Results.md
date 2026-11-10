# Phase 2 Results

P2 EVAL-PREP: AGB-DE turned into input clauses for the agent and a separate answer key.
Numbers below are from the pinned corpus commit `9b847807`.

**Status:** complete. Steps 2.1–2.5 implemented and gated, 2.6 built and run — see
[The retrieval re-check](#the-retrieval-re-check-26), whose result is the handoff to
Phase 4.

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

## The retrieval re-check (2.6)

Phase 1 scored **primary@5 0.933** against 30 synthetic queries written by Claude,
and `data/gold/README.md` committed to re-running that check on real clause text,
naming it the number to trust. The re-run scores **primary@5 0.258** over 31 real
void clauses.

The 0.675 gap is the measure of how much of Phase 1's score came from the test
being easy, and it is recorded rather than gated: failing a run on it would punish
the honest number.

### How the key was built

The design doc proposed a `subtopic -> chunk_id` mapping. That column is nearly
empty in our slice - 16 of 523 clauses carry one, 14 in the dev half, across 5
values, with `warranty:period` alone accounting for 8 and pointing at a single
chunk. Roughly five effective queries, which cannot distinguish a working index
from a lucky one.

The key is built instead from the **31 void dev-half clauses**, spanning 11
distinct chunks. The void annotation is the authors'; the paragraph that makes
each clause void was written by Claude and is the one link in the chain that is
not independent. Eight mappings are flagged `confidence: low` in
`configs/gold_map.yaml`; three of those are noted as needing a lawyer.

### The number decomposes into two causes

```
  k    all 31   reachable 24
  1     0.000          0.000
  3     0.129          0.167
  5     0.258          0.333
 10     0.645          0.833
 20     0.710          0.917
```

**Cause 1 - the topic filter hides the answer (7 of 31, all unreachable).**
`_topic_filter` in `p1_kb/s5_check_retrieval.py` admits chunks tagged with the
query topic or `general`. Two consequences:

- Chunks tagged `other` are invisible to **every** query. § 308 Nr. 4 and
  § 309 Nr. 3 can never be retrieved by anything, which is a live defect in the
  shipped retrieval path, not only in this test.
- Cross-topic answers are cut: a liability clause whose basis is § 475 Abs. 2 or
  § 439 Abs. 2, both tagged `warranty` only, is unreachable by construction.

Affected: `agb-187`, `agb-344`, `agb-354`, `agb-901`, `agb-1464`, `agb-1607`,
`agb-3490`. Excluding them, primary@5 is 0.333.

**Cause 2 - register mismatch (14 reachable misses, every one at rank 6-13).**
11 of those 14 retrieved a *substantive* norm at rank 1 where the answer was a
§ 308/309 prohibition:

> `agb-2802` "Haftung ist auf Vorsatz und grobe Fahrlässigkeit begrenzt"
> retrieved § 276 Abs. 1 ("Der Schuldner hat Vorsatz und Fahrlässigkeit zu
> vertreten"); expected § 309 Nr. 7 lit. a ("unwirksam ist eine Bestimmung,
> durch die...").

Semantic similarity finds the norm a clause is *about*, not the norm that makes
it *void*. The two are written in different registers and share little
vocabulary. The correct chunk is usually found - it simply ranks below the
substantive norms.

### A caveat about the key itself

The answer key deliberately names the prohibition, because that is what a
due-diligence reviewer must cite. A key built the other way - "the statutory rule
the clause deviates from" - would score substantially higher against the same
index. Part of the low number reflects genuine difficulty in the task, not only a
weakness of the retriever. Both readings are defensible; ours follows the product.

### Handoff to Phase 4

Phase 1 fixed the order of remedies: topic tagging, chunk granularity,
hybrid/BM25, embedding model. Topic tagging is first on that list and is exactly
what cause 1 is. The three candidates, in cost order and all untested:

1. Treat § 305-310 as always-included, as `general` already is, so `other` stops
   hiding chunks. Addresses all 7 unreachable cases; a tagging change only.
2. Raise the agent's `top_k` from 5 to 10. Reachable @10 is 0.833, and the judge
   model can discriminate among 10 candidates better than the embedder can rank
   them.
3. Hybrid / BM25, which targets cause 2 directly: "unwirksam" and "ausgeschlossen"
   are lexical signals a dense model dilutes.

**Baseline to beat: primary@5 0.258 over all 31, 0.333 over the 24 reachable.**
Rebuild with `make recheck` after any change to `configs/gold_map.yaml`, the
chunker, or the topic tags.

### A note on `reports/p2_counts.json`

The committed counts file predates 2.6 and so carries no `retrieval_recheck`
block; the re-check was run through `make recheck`, which writes
`reports/p2_retrieval_recheck.json` only. The next full `make eval-data` will
populate both.

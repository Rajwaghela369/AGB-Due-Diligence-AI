# Gold retrieval queries

`kb_queries.jsonl` is the answer key for the Phase 1 retrieval gate: 30 clause-style
German AGB texts, each with the BGB chunk a reviewer should expect back.

| Field | Meaning |
| --- | --- |
| `query` | The clause text used as the search query |
| `topic` | What the router would classify it as; drives the topic filter |
| `primary_chunk_id` | The chunk the gate scores against |
| `also_relevant` | Further defensible grounds; diagnostic, not scored |

## Provenance, and its limits

These queries and labels were **written by Claude, not by a lawyer**, and the clause
texts are synthetic rather than drawn from real shop terms. The gate therefore shows
that the index is structurally sound, not that it performs on real input.

They were written deliberately **before the chunker existed** (task 1.2e precedes 1.3),
so the labels could not be fitted to whatever the chunker happened to produce. All 30
predicted chunk ids resolved against the first index with no misses, which is evidence
the ID scheme and the chunking rules agreed with what was committed beforehand.

## Corrections after the first gate run

Three labels were changed **after** seeing the first retrieval results (primary@5 0.87).
This weakens their independence and is recorded here rather than left implicit.

| Query | Was | Now | Reason |
| --- | --- | --- | --- |
| `gq-13` | `bgb-307-abs1` | `bgb-309-7-a` | A blanket exclusion of all liability is void under § 309 Nr. 7 because it also covers life, body, health and gross negligence. Transparency under § 307 is an additional ground, not the lead one. The original label had the two the wrong way round. |
| `gq-30` | `bgb-474-abs1` | `bgb-310-abs3` | § 310 Abs. 3 governs standard terms in consumer contracts; § 474 Abs. 1 only defines Verbrauchsgüterkauf. |
| `gq-19` | also: none | also: `bgb-309-8-b` | The clause forfeits claims through a five-day notice deadline, which is § 309 Nr. 8 lit. b sublit. ee. That ground was omitted. |

Each correction was accepted only if it held up on legal grounds independently of what
retrieval returned. That test cannot fully rule out hindsight, which is why the number
to trust is the Phase 2 re-run: AGB-DE's expert `subtopics` yield labels on real clause
text that neither the author of this file nor the model produced.

`gq-05` was left as a genuine miss. Its clause excludes liability for anything but
intent without using the words "grobe Fahrlässigkeit", so reaching § 309 Nr. 7 lit. b
needs an inference the embedding does not make.


---

# Real-clause queries (Phase 2)

`agb_queries.jsonl` is the Phase 2 re-check this file promised: 31 **real** AGB
clauses, each one the authors of AGB-DE labelled void, paired with the BGB chunk
that makes it void. Same schema and same scorer as `kb_queries.jsonl`, so the two
numbers are directly comparable.

It is **generated**, not hand-edited. `configs/gold_map.yaml` holds the
hand-authored part (chunk ids, reasoning, confidence); `pipelines/p2_eval/s6_regen_gold.py`
joins it onto `data/processed/splits/train.parquet` so the query text is always
verbatim from the corpus. Rebuild with `make recheck`.

## Provenance, and where it still falls short

| Link | Source | Independent of us? |
| --- | --- | --- |
| The clause text | AGB-DE, verbatim | yes |
| "this clause is void" | AGB-DE's annotators | yes |
| "void under **this** §" | Claude, reviewed by the author | **no** |

Phase 1's set was synthetic on all three links. This is independent on two. A
fully independent key needs a lawyer for the third, and that is the ceiling of
what this file can claim.

## Scope, and why these 31

Dev half only — the held-out 104 clauses stay sealed until Phase 3.

The design doc proposed deriving the key from AGB-DE's `subtopics` column. That
column is nearly empty in our slice: 16 of 523 clauses carry one, 14 of them in
the dev half, across 5 distinct values, with `warranty:period` alone accounting
for 8 and pointing at a single chunk. Roughly five effective queries, which
cannot distinguish a working index from a lucky one. The `void` flag is the
denser expert signal, so the key is built from the 31 void dev clauses instead
and spans 11 distinct chunks.

## Eight mappings are flagged `confidence: low`

`agb-348`, `agb-354`, `agb-719`, `agb-901`, `agb-1360`, `agb-1464`, `agb-1607`,
`agb-3490`. Read these first; `note` in the mapping says what the doubt is. Three
are worth singling out:

- **`agb-1464` / `agb-1607`** — set-off clauses that, read literally, permit
  set-off with undisputed or finally adjudicated claims, which is exactly what
  § 309 Nr. 3 protects. On their face they comply. The annotators marked them
  void and the text alone does not explain why.
- **`agb-1360`** — void because Art. 6(2) Rome I keeps German warranty law
  applicable despite the Italian choice of law. Rome I is not in the index, so
  no fully correct chunk exists. A legitimate miss.
- **`agb-354`** — § 439 Abs. 2 if read as a Nacherfüllung return, § 357 if read
  as a withdrawal return. § 357 is not in the index either.

## What a drop against 0.933 means

Expected, and informative rather than a failure — which is why the flow records
this number and never gates on it. Phase 1's queries were short, clean and
written against a known index; these have a median length of 372 characters and a
maximum of 1938, carry `<<NAME>>` placeholders, and several bundle three legal
problems into one paragraph. The gap between the two scores is the measure of how
much of 0.933 came from the test being easy.

# Phase 1 results — BGB knowledge base

Nov 5, 2026

The gate passed. `bgb_agb_v1` holds 105 chunks from 31 BGB paragraphs, built by
`make kb` from a pinned HuggingFace revision and validated against 30 gold queries.

## Numbers

| Metric | Value |
| --- | --- |
| BGB rows snapshotted | 2,538 of 113,537 |
| Paragraphs resolved | 31 of 31 |
| Chunks | 105 |
| primary@1 | 0.433 |
| primary@3 | 0.767 |
| **primary@5** | **0.933** (gate 0.90) |
| MRR | 0.629 |
| Index tokens | 15,585 |
| Query tokens | 1,111 |

Source: Open Legal Data `openlegaldata/laws-germany`, config `dump-20260520`,
revision `ad694182262416ad204f43d0c13ca0a7dd12057e`. Embeddings:
`jina-embeddings-v5-text-small`, 1024 dimensions, `retrieval.passage` for indexing
and `retrieval.query` for search.

## What the spikes settled

- **Item markup survives.** The `content` field is XML-derived markup with `<P>` for
  Absatz and `<DL>/<DT>/<DD>/<LA>` for numbered items, so chunking works off real
  structure rather than regex over prose.
- **The text is current.** § 434 carries the post-2022 Kaufrecht wording, so no
  paragraph needed hand-patching. This was the risk rated highest before the spike.
- **`section` reads `"§ 309"`, not `"309"`.** The dataset card showed otherwise. All 31
  whitelist refs match the dump verbatim, so no rekeying was needed.

## Known limitations

**No statutory validity date.** The dataset carries the aggregator's `created_date` and
`updated_date`, not the law's Stand. There is no way to tell from it which version of
the BGB a record represents. The HF revision is recorded in the manifest, which
identifies the snapshot but not the legal state. A lawyer will eventually ask "which
version of the BGB was this checked against", and the honest answer today is "the one
Open Legal Data published at revision ad694182".

**The gold set is synthetic and not lawyer-verified.** 30 clause-style queries written
by Claude, with labels Claude assigned. Three were corrected after the first gate run;
`data/gold/README.md` records which and why. This gate shows the index is structurally
sound, not that it performs on real clause text. Phase 2 re-runs the same check against
AGB-DE train clauses using the annotators' `subtopics`, and that is the number to trust.

**primary@1 is 0.433.** The expected chunk often ranks second or third. Partly inherent
— a clause usually has several defensible norms — but if the Phase 4 judge sees only the
top 3, ranking quality matters more than primary@5 suggests.

**One unresolved retrieval gap.** `gq-05` excludes liability for everything but intent
without using the words "grobe Fahrlässigkeit", so § 309 Nr. 7 lit. b ranks 11th.
Keyword search would not help; there is no shared rare term. Retrieval does surface
§§ 276 and 280, so the likely fix is the judge reasoning from those rather than better
retrieval.

## Whitelist review (task W4)

The four speculative additions all earned their place:

| Paragraph | Top-5 appearances | Gold primary |
| --- | --- | --- |
| § 276 | 14 | gq-10, rank 1 |
| § 280 | 15 | — |
| § 278 | 3 | gq-06, rank 2 |
| § 202 | 1 | gq-11, rank 3 |

§§ 305a, 305b, 306a, 308 and 474 never reached a top 5. That reflects gaps in a
30-query gold set rather than dead weight: none of the queries target
Klauselverbote mit Wertungsmöglichkeit, yet § 308 contributes 11 chunks that real AGB
will trigger. Nothing is pruned.

**Worth watching:** § 280 appears in half of all top-5 lists and is never the correct
answer. It may be costing precision rather than adding evidence. Phase 2's re-run on
real clauses will show whether that holds.

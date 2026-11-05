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

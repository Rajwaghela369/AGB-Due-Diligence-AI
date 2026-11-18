# AVV gold set — labelling rules

## Purpose

This set tests clause-level recognition of the eight contractual obligations in
Art. 28 Abs. 3 lit. a–h DSGVO. It contains 40 German clauses: 32 verbatim fragments
from the European Commission's controller–processor standard clauses and eight
clearly marked counterfactual negatives derived from those fragments.

The set is a technical evaluation aid, not legal advice or a lawyer-verified answer key.

## Unit of annotation

One row is one independently assessable source fragment. A fragment may fulfil more
than one letter. Every row receives exactly one status for every letter a–h:

- `fulfilled`: the fragment states the substance of that letter as a binding processor duty;
- `failed`: the fragment expressly contradicts, disclaims, or removes that duty;
- `silent`: the fragment neither fulfils nor contradicts that duty.

Mere references to the DSGVO, general promises of compliance, enforcement remedies,
or contract metadata do not fulfil a letter. A clause is not marked `failed` merely
because it is silent; absence is evaluated later by the document-level missing-obligation
check. The Art. 28 Abs. 3 second-subparagraph duty to flag unlawful instructions is
labelled under `h`, because the statutory text expressly attaches it to lit. h.

## Multi-label decisions

Labels are independent. Access restriction plus personnel confidentiality can fulfil
both `b` and `c`; concrete measures supporting both data-subject requests and other
controller duties can fulfil both `e` and `f`. `fulfilled`, `failed`, and `silent` lists
in the generated JSONL are derived from the complete `labels` mapping and are not a
second answer key.

## Counterfactual negatives

The official template is designed to comply with Art. 28 and therefore supplies no
useful express failures. Eight rows invert exactly one official duty, one per letter.
They are marked `source.verbatim=false`, carry the transformation in
`source.adaptation`, and label only the inverted letter `failed`. They must never be
presented as wording issued or endorsed by the European Commission.

## Split

The fixed split contains 30 development rows and 10 held-out rows. Both sides contain
verbatim and counterfactual rows; the held-out side has eight verbatim rows and two
counterfactual failures. This is a clause-level split from one template, so it measures
new-clause performance, not generalisation to a new drafting organisation. Only the
held-out rows are prepared for the Langfuse dataset.

## Record shape

`annotations.jsonl` is the human-maintained decision layer. Running
`python -m pipelines.p2_eval.avv_dataset` joins it to the pinned source and writes
`avv_clauses.jsonl`. Each generated row includes the text, split, complete a–h labels,
derived status lists, label notes, source citation, verbatim/adaptation marker, retrieval
date, and reuse terms.

## Sources and reuse terms

1. Regulation (EU) 2016/679, CELEX `32016R0679`, official German XHTML from the EU
   Publications Office; used for Art. 4 definitions and Arts. 28 and 32 norm chunks.
2. Commission Implementing Decision (EU) 2021/915, CELEX `32021D0915`, official German
   controller–processor standard contractual clauses; used for the labelled clauses.

Both sources are covered by the European Commission reuse policy under Decision
2011/833/EU, subject to acknowledgement of the source and the policy's other conditions:
<https://commission.europa.eu/legal-notice_en#copyright-notice>. Canonical source URLs
and retrieval hashes are stored in `data/raw/dsgvo_avv/*_source.json`.

## Reproduction and external writes

Refresh and rebuild local artifacts with:

```bash
python -m pipelines.p1_kb.dsgvo_avv --fetch
python -m pipelines.p2_eval.avv_dataset --fetch
```

Neither command calls Jina, Qdrant, Gemini, or Langfuse. Live indexing requires both
`--build-index --approve-live-index`; Langfuse publication requires both
`--upload --approve-live-upload`. Those flags are reserved for an orchestrator-approved
run with configured credentials.

## Known limitations

- The labels were produced for this project and have not been reviewed by a lawyer.
- One official template cannot measure drafting-source generalisation.
- Counterfactual negatives improve failure coverage but are synthetic and linguistically
  simpler than defects in real agreements.
- No AVV F1 is recorded until an approved model evaluation is run against the held-out set.

-- Phase 8: the optional LLM recommendation paragraph of the summary block (8.A, behind
-- `summary.llm_enabled`). Keyed by the summary's decision set hash and the model, like every
-- other paid lookup, so a paragraph is generated at most once per decision set; a changed
-- decision changes the hash, and the old row simply stops being read.

create table summary_texts (
    decision_set_hash text not null,
    model             text not null,
    text              text not null,
    created_at        timestamptz not null default clock_timestamp(),
    primary key (decision_set_hash, model)
);

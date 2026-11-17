-- Phase 7: router topics, assigned when a review starts rather than at ingest, so ingest
-- stays offline and deterministic. The classification is cached by clause text and router
-- model like every other paid lookup; the review keeps the topic it used on its own rows,
-- so a resumed or reloaded review scopes exactly as it did when it started.

create table topics (
    content_hash text not null,
    model        text not null,
    topic        text not null,
    created_at   timestamptz not null default clock_timestamp(),
    primary key (content_hash, model)
);

alter table review_clauses add column topic text;

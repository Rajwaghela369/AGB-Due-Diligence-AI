-- Phase 10: a document's profile is chosen at upload and copied to its reviews. AVV
-- routing is multi-label because one clause can satisfy several Art. 28(3) obligations.

alter table uploads
    add column document_type text not null default 'agb';

alter table documents
    add column document_type text not null default 'agb';

alter table reviews
    add column document_type text not null default 'agb',
    add column obligations text[] not null default '{}';

alter table review_clauses
    add column obligations text[] not null default '{}';

alter table verdicts
    add column obligation_findings jsonb not null default '[]';

create table obligation_routes (
    content_hash text not null,
    model        text not null,
    profile      text not null,
    obligations text[] not null default '{}',
    created_at   timestamptz not null default clock_timestamp(),
    primary key (content_hash, model, profile)
);

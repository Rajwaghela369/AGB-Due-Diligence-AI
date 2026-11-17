-- Orphan sweeping waits an hour so an ingest that has written a clause text but not yet
-- attached its document cannot race the nightly job. Other content-addressed tables
-- already have created_at; clause_texts predates that safeguard.

alter table clause_texts
    add column created_at timestamptz not null default clock_timestamp();


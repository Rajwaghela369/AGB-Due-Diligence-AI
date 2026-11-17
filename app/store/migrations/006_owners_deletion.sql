-- Phase 9: owners (9.E), reference-counted deletion (9.B) and retention (9.C).
-- docs/Phase 9 Design.md explains each part; docs/Phase 9 API.md is the contract.

-- 1. Owners. Every existing row belongs to the configured default owner: migrate.py sets
-- `agb.default_owner` from `auth.default_owner` for the migration's transaction, and a run
-- without it (psql, an old caller) falls back to 'local', app.domain.DEFAULT_OWNER_ID.
-- `source_hash` is the sha256 of the uploaded bytes, the stem of the blob key; until now
-- the document id was exactly that.
alter table documents
    add column owner_id    text,
    add column source_hash text;
update documents
set owner_id = coalesce(nullif(current_setting('agb.default_owner', true), ''), 'local'),
    source_hash = document_id;
alter table documents
    alter column owner_id set not null,
    alter column source_hash set not null;
create index documents_owner_idx on documents (owner_id, created_at desc);
create index documents_source_hash_idx on documents (source_hash);

alter table reviews add column owner_id text;
update reviews r
set owner_id = coalesce(
    (select d.owner_id from documents d where d.document_id = r.document_id),
    nullif(current_setting('agb.default_owner', true), ''),
    'local'
);
alter table reviews alter column owner_id set not null;
create index reviews_owner_idx on reviews (owner_id, created_at desc);

-- The uploader of each upload event, so a per-user deletion can find its own rows.
alter table uploads add column owner_id text;
update uploads u
set owner_id = coalesce(
    (select d.owner_id from documents d where d.document_id = u.document_id),
    nullif(current_setting('agb.default_owner', true), ''),
    'local'
);

-- 2. Deletion may remove decision events. The history stays append-only for everything
-- else: a deletion transaction alone sets `agb.erasure = 'on'` (`set local`, so it ends
-- with the transaction), and only DELETE is let through. UPDATE and TRUNCATE stay refused.
create or replace function decision_events_append_only() returns trigger
language plpgsql as $$
begin
    if tg_op = 'DELETE' and current_setting('agb.erasure', true) = 'on' then
        return old;
    end if;
    raise exception 'decision_events is append-only (% refused)', tg_op
        using errcode = 'insufficient_privilege';
end
$$;

-- 3. Tombstones: what the audit trail keeps of a deletion. No content: ids (hashes) and
-- counts only. Append-only like decision_events, with no erasure exception.
create table deletion_tombstones (
    tombstone_id  text primary key,
    kind          text not null check (kind in ('document', 'review')),
    subject_id    text not null,
    owner_id      text not null,
    deleted_by    text,
    authenticated boolean not null default false,
    reason        text not null check (reason in ('user', 'retention', 'orphan_sweep')),
    removed       jsonb not null default '{}',
    at            timestamptz not null default clock_timestamp()
);
create index deletion_tombstones_subject_idx on deletion_tombstones (subject_id, at);

create function deletion_tombstones_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'deletion_tombstones is append-only (% refused)', tg_op
        using errcode = 'insufficient_privilege';
end
$$;

create trigger deletion_tombstones_no_update_delete
    before update or delete on deletion_tombstones
    for each row execute function deletion_tombstones_append_only();

create trigger deletion_tombstones_no_truncate
    before truncate on deletion_tombstones
    for each statement execute function deletion_tombstones_append_only();

-- 4. The erasure outbox: what lives outside the database (blobs, Langfuse traces, scores
-- and dataset items, LangGraph checkpoints) is queued in the deletion's own transaction
-- and deleted after the commit, retried until done. A crash between the commit and the
-- external delete therefore loses nothing: the task is still pending.
create table erasure_tasks (
    task_id      bigint generated always as identity primary key,
    target       text not null
        check (target in ('blob', 'trace', 'score', 'dataset_item', 'checkpoint')),
    key          text not null,
    tombstone_id text references deletion_tombstones (tombstone_id),
    requested_at timestamptz not null default clock_timestamp(),
    attempts     integer not null default 0,
    last_error   text,
    done_at      timestamptz
);
-- One pending task per thing to erase; a second deletion naming it reuses the first.
create unique index erasure_tasks_pending_idx on erasure_tasks (target, key)
    where done_at is null;

-- 5. A summary paragraph is keyed by its decision set hash, which a deletion cannot
-- recompute for earlier decision sets. The review it was generated for makes it
-- deletable; null on rows written before Phase 9 (the retention sweep removes those).
alter table summary_texts add column review_id text;
create index summary_texts_review_idx on summary_texts (review_id);

-- 6. Retention reads these.
create index uploads_uploaded_at_idx on uploads (document_id, uploaded_at desc);
create index reviews_finished_idx on reviews (finished_at) where finished_at is not null;

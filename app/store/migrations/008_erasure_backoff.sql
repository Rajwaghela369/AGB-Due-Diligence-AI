-- Phase 9 review fixes (9.B): erasure backoff and sweep tombstones.

-- 1. A failed erasure task waits before it is due again (exponential backoff, set by
-- `finish_erasure`), and the worker reads due tasks by `next_attempt_at`. Ordering by
-- `requested_at` alone kept failing tasks at the head of the queue, so after a batch of
-- them newer tasks were never reached.
alter table erasure_tasks
    add column next_attempt_at timestamptz not null default clock_timestamp();
create index erasure_tasks_due_idx on erasure_tasks (next_attempt_at, task_id)
    where done_at is null;

-- 2. The orphan sweep records what it removed, like a deletion does (reason
-- 'orphan_sweep'); its subject is not a document or review, hence the new kind.
alter table deletion_tombstones drop constraint deletion_tombstones_kind_check;
alter table deletion_tombstones add constraint deletion_tombstones_kind_check
    check (kind in ('document', 'review', 'sweep'));

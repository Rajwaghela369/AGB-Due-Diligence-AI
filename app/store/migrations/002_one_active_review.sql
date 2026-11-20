-- In-flight dedupe in the database, not only in one process: at most one queued
-- or running review per document and agent version. Two API processes (or an old
-- and a new instance overlapping during a restart) can both find no active review
-- and insert; the second insert now conflicts and the repository returns the
-- review that won.
create unique index reviews_one_active_idx
    on reviews (document_id, agent_version)
    where status in ('queued', 'running');

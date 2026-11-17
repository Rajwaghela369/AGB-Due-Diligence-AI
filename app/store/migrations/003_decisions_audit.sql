-- Phase 7: the decision audit trail and lawyer agreement.
--
-- `decisions` keeps one current row per review and position; `decision_events` is its
-- history, one row per decision ever made, written in the same transaction as the upsert
-- (app.store.postgres.PostgresDecisionRepo.put). The history is evidence of who decided
-- what and when, so the database itself refuses to change it: a trigger rejects every
-- update, delete and truncate, whoever the role is.

alter table decisions
    add column model_verdict      text,
    add column model_basis        text[] not null default '{}',
    add column reason_de          text not null default '',
    add column reason_unavailable boolean not null default false;
-- (`decided_by` has been on `decisions` since 001; it now holds the reviewer's name.)

-- Decisions made before this migration: take the model's verdict from the verdict cache,
-- through the review's document and position. A clause re-split since then may not match;
-- such rows stay null and the agreement view leaves them out.
update decisions d
set model_verdict = v.verdict, model_basis = v.legal_basis
from reviews r
join document_clauses c on c.document_id = r.document_id
join verdicts v on v.content_hash = c.content_hash and v.agent_version = r.agent_version
where d.review_id = r.review_id and c.position = d.position and d.model_verdict is null;

create table decision_events (
    event_id        bigint generated always as identity primary key,
    review_id       text not null,
    position        integer not null,
    action          text not null,
    previous_action text,
    from_verdict    text,
    from_basis      text[] not null default '{}',
    to_verdict      text,
    to_basis        text[] not null default '{}',
    note            text not null default '',
    decided_by      text,
    authenticated   boolean not null default false,
    agent_version   text not null,
    request_id      text,
    at              timestamptz not null default clock_timestamp()
);
create index decision_events_review_idx on decision_events (review_id, position, event_id);

create function decision_events_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'decision_events is append-only (% refused)', tg_op
        using errcode = 'insufficient_privilege';
end
$$;

create trigger decision_events_no_update_delete
    before update or delete on decision_events
    for each row execute function decision_events_append_only();

create trigger decision_events_no_truncate
    before truncate on decision_events
    for each statement execute function decision_events_append_only();

-- Agreement per agent version and model verdict over current decisions. `kept` counts an
-- override that changes only the § as agreement; a dismissal is neither (Phase 7 plan, 7.C).
create view decision_agreement as
select
    r.agent_version,
    d.model_verdict,
    count(*) as decided,
    count(*) filter (
        where d.action = 'accept'
           or (d.action = 'override' and d.corrected_verdict = d.model_verdict)
    ) as kept,
    count(*) filter (
        where d.action = 'override' and d.corrected_verdict is distinct from d.model_verdict
    ) as overridden,
    count(*) filter (where d.action = 'dismiss') as dismissed,
    max(d.decided_at) as last_decided_at
from decisions d
join reviews r on r.review_id = d.review_id
where d.model_verdict is not null
group by r.agent_version, d.model_verdict;

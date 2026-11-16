-- Phase 5 application tables. Content-addressed where the data is: a clause text,
-- a verdict, an embedding or a retrieval is stored once however many documents
-- and reviews use it. The evaluation `clauses` table is not touched here; it
-- stays the answer key, and these tables never reference it.

-- The evaluation harness wrote `verdicts` keyed on the eval clause id
-- (`agb-1039`) with a `lawyer_decision` column. Those rows are the record of
-- the Phase 3/4 evaluation runs and cannot be re-keyed (a clause id says nothing
-- about the text without a join), so the table moves aside rather than being
-- dropped. A fresh database has no such table and this block does nothing.
do $$
begin
    if exists (
        select 1 from information_schema.columns
        where table_schema = current_schema()
          and table_name = 'verdicts' and column_name = 'clause_id'
    ) then
        alter table verdicts rename to verdicts_legacy_eval;
        alter table verdicts_legacy_eval
            rename constraint verdicts_pkey to verdicts_legacy_eval_pkey;
    end if;
end
$$;

create table uploads (
    upload_id    text primary key,
    document_id  text not null,
    filename     text not null,
    source_type  text not null,
    size         bigint not null,
    uploaded_by  text,
    uploaded_at  timestamptz not null default clock_timestamp()
);
create index uploads_document_idx on uploads (document_id);

create table documents (
    document_id      text primary key,
    filename         text not null,
    source_type      text not null,
    page_count       integer,
    parser_version   text not null,
    splitter_version text not null,
    text_hash        text not null,
    created_at       timestamptz not null default clock_timestamp()
);

create table clause_texts (
    content_hash text primary key,
    text         text not null
);

create table document_clauses (
    document_id  text not null references documents (document_id) on delete cascade,
    position     integer not null,
    number       text,
    heading      text,
    content_hash text not null,
    page         integer,
    rects        jsonb not null default '[]',
    start_offset integer,
    end_offset   integer,
    in_scope     boolean not null default true,
    topic        text,
    primary key (document_id, position)
);
create index document_clauses_hash_idx on document_clauses (content_hash);

create table verdicts (
    content_hash       text not null,
    agent_version      text not null,
    verdict            text not null,
    legal_basis        text[] not null default '{}',
    candidates         text[] not null default '{}',
    basis_scores       jsonb not null default '{}',
    reason_de          text not null default '',
    reason_unavailable boolean not null default false,
    confidence         double precision not null,
    void_probability   double precision,
    check_rule         text,
    trace_id           text,
    observations       jsonb not null default '{}',
    input_tokens       integer not null default 0,
    output_tokens      integer not null default 0,
    latency_ms         integer,
    created_at         timestamptz not null default clock_timestamp(),
    primary key (content_hash, agent_version)
);

create table retrievals (
    content_hash text not null,
    index_hash   text not null,
    policy_hash  text not null,
    candidates   jsonb not null,
    created_at   timestamptz not null default clock_timestamp(),
    primary key (content_hash, index_hash, policy_hash)
);

create table embeddings (
    content_hash text not null,
    model        text not null,
    vector       double precision[] not null,
    created_at   timestamptz not null default clock_timestamp(),
    primary key (content_hash, model)
);

create table reviews (
    review_id     text primary key,
    document_id   text not null,
    agent_version text not null,
    scope         text[] not null,
    status        text not null,
    counts        jsonb not null default '{}',
    cost_usd      double precision not null default 0,
    trace_id      text,
    error         text,
    created_at    timestamptz not null default clock_timestamp(),
    started_at    timestamptz,
    finished_at   timestamptz
);
create index reviews_document_idx on reviews (document_id, created_at desc);
create index reviews_status_idx on reviews (status);

create table review_clauses (
    review_id text not null references reviews (review_id) on delete cascade,
    position  integer not null,
    status    text not null,
    error     text,
    primary key (review_id, position)
);

create table run_events (
    review_id  text not null,
    seq        integer not null,
    type       text not null,
    payload    jsonb not null,
    created_at timestamptz not null default clock_timestamp(),
    primary key (review_id, seq)
);

create table decisions (
    review_id         text not null,
    position          integer not null,
    action            text not null,
    corrected_verdict text,
    corrected_basis   text[] not null default '{}',
    note              text not null default '',
    decided_by        text,
    decided_at        timestamptz not null default clock_timestamp(),
    primary key (review_id, position)
);

create table feedback (
    feedback_id       text primary key,
    review_id         text not null,
    position          integer not null,
    agent_version     text not null,
    step              text,
    observation_id    text,
    vote              text not null,
    reasons           text[] not null default '{}',
    corrected_verdict text,
    corrected_basis   text[] not null default '{}',
    comment           text not null default '',
    source            text not null default 'vote',
    created_at        timestamptz not null default clock_timestamp()
);
create index feedback_review_idx on feedback (review_id, position, created_at desc);

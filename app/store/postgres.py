"""Postgres implementations of the store protocols, over `core.db.connection()`.

Each method is one short transaction on its own connection, which keeps the
repositories thread-safe without sharing a connection between worker threads.
Content-addressed writes are `insert ... on conflict do nothing` followed by a
select, so the first writer wins and every caller gets the stored row back.
Array columns hold lists of ids; rects, observations and payloads are JSONB.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from core.db import connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import (
    REASON_STEP,
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Review,
    ReviewCounts,
    ReviewStatus,
    StepObservations,
    StoredEvent,
)
from app.store.files import LocalFileStore
from app.store.interfaces import FileStore

Connect = Callable[[], AbstractContextManager[Any]]


def _rows(conn: Any, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _row(conn: Any, sql: str, params: tuple = ()) -> dict | None:
    rows = _rows(conn, sql, params)
    return rows[0] if rows else None


class _Repo:
    def __init__(self, connect: Connect = connection) -> None:
        self._connect = connect


class PostgresDocumentRepo(_Repo):
    def record_upload(self, document_id: str, filename: str, source_type: str, size: int) -> str:
        upload_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "insert into uploads (upload_id, document_id, filename, source_type, size)"
                " values (%s, %s, %s, %s, %s)",
                (upload_id, document_id, filename, source_type, size),
            )
        return upload_id

    @staticmethod
    def _document(row: dict) -> Document:
        return Document(**row)

    _DOC_COLUMNS = (
        "document_id, filename, source_type, page_count, parser_version, splitter_version,"
        " text_hash, created_at"
    )

    def get(self, document_id: str) -> Document | None:
        with self._connect() as conn:
            row = _row(
                conn,
                f"select {self._DOC_COLUMNS} from documents where document_id = %s",
                (document_id,),
            )
        return self._document(row) if row else None

    def find_parsed(
        self, document_id: str, parser_version: str, splitter_version: str
    ) -> Document | None:
        doc = self.get(document_id)
        if doc and (doc.parser_version, doc.splitter_version) == (parser_version, splitter_version):
            return doc
        return None

    def save(self, document: Document, clauses: list[ClauseSpan], texts: dict[str, str]) -> None:
        with self._connect() as conn:
            for clause in clauses:
                conn.execute(
                    "insert into clause_texts (content_hash, text) values (%s, %s)"
                    " on conflict (content_hash) do nothing",
                    (clause.content_hash, texts.get(clause.content_hash, clause.text)),
                )
            # Serialise concurrent saves of the same document; the second sees the first's rows.
            conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (document.document_id,)
            )
            existing = _row(
                conn,
                "select parser_version, splitter_version from documents where document_id = %s",
                (document.document_id,),
            )
            if existing and (
                existing["parser_version"] == document.parser_version
                and existing["splitter_version"] == document.splitter_version
            ):
                return  # same bytes under the same versions: reuse
            # A new parser or splitter version replaces the earlier parse of the same bytes.
            conn.execute(
                "insert into documents (document_id, filename, source_type, page_count,"
                " parser_version, splitter_version, text_hash, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))"
                " on conflict (document_id) do update set"
                " filename = excluded.filename, source_type = excluded.source_type,"
                " page_count = excluded.page_count, parser_version = excluded.parser_version,"
                " splitter_version = excluded.splitter_version, text_hash = excluded.text_hash",
                (
                    document.document_id,
                    document.filename,
                    document.source_type,
                    document.page_count,
                    document.parser_version,
                    document.splitter_version,
                    document.text_hash,
                    document.created_at,
                ),
            )
            conn.execute(
                "delete from document_clauses where document_id = %s", (document.document_id,)
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into document_clauses (document_id, position, number, heading,"
                    " content_hash, page, rects, start_offset, end_offset, in_scope, topic)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            document.document_id,
                            c.position,
                            c.number,
                            c.heading,
                            c.content_hash,
                            c.page,
                            Jsonb([r.model_dump() for r in c.rects]),
                            c.start,
                            c.end,
                            c.in_scope,
                            c.topic,
                        )
                        for c in clauses
                    ],
                )

    def clauses(self, document_id: str) -> list[ClauseSpan]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select c.position, c.number, c.heading, t.text, c.content_hash, c.page,"
                ' c.rects, c.start_offset as start, c.end_offset as "end", c.in_scope, c.topic'
                " from document_clauses c join clause_texts t using (content_hash)"
                " where c.document_id = %s order by c.position",
                (document_id,),
            )
        return [ClauseSpan(**row) for row in rows]

    def list(self, limit: int = 100) -> list[Document]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {self._DOC_COLUMNS} from documents order by created_at desc limit %s",
                (limit,),
            )
        return [self._document(r) for r in rows]


_VERDICT_COLUMNS = (
    "content_hash, agent_version, verdict, legal_basis, candidates, basis_scores, reason_de,"
    " reason_unavailable, confidence, void_probability, check_rule, trace_id, observations,"
    " input_tokens, output_tokens, latency_ms"
)


def _verdict(row: dict) -> ClauseVerdict:
    row = dict(row)
    row["check"] = row.pop("check_rule")
    row["observations"] = StepObservations(**row["observations"])
    return ClauseVerdict(**row)


class PostgresVerdictRepo(_Repo):
    def get(self, content_hash: str, agent_version: str) -> ClauseVerdict | None:
        with self._connect() as conn:
            row = _row(
                conn,
                f"select {_VERDICT_COLUMNS} from verdicts"
                " where content_hash = %s and agent_version = %s",
                (content_hash, agent_version),
            )
        return _verdict(row) if row else None

    def save(self, verdict: ClauseVerdict) -> ClauseVerdict:
        with self._connect() as conn:
            conn.execute(
                f"insert into verdicts ({_VERDICT_COLUMNS})"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (content_hash, agent_version) do nothing",
                (
                    verdict.content_hash,
                    verdict.agent_version,
                    verdict.verdict,
                    verdict.legal_basis,
                    verdict.candidates,
                    Jsonb(verdict.basis_scores),
                    verdict.reason_de,
                    verdict.reason_unavailable,
                    verdict.confidence,
                    verdict.void_probability,
                    verdict.check,
                    verdict.trace_id,
                    Jsonb(verdict.observations.model_dump()),
                    verdict.input_tokens,
                    verdict.output_tokens,
                    verdict.latency_ms,
                ),
            )
            row = _row(
                conn,
                f"select {_VERDICT_COLUMNS} from verdicts"
                " where content_hash = %s and agent_version = %s",
                (verdict.content_hash, verdict.agent_version),
            )
        return _verdict(row)


class PostgresCacheRepo(_Repo):
    def get_embedding(self, content_hash: str, model: str) -> list[float] | None:
        with self._connect() as conn:
            row = _row(
                conn,
                "select vector from embeddings where content_hash = %s and model = %s",
                (content_hash, model),
            )
        return list(row["vector"]) if row else None

    def put_embedding(self, content_hash: str, model: str, vector: list[float]) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into embeddings (content_hash, model, vector) values (%s, %s, %s)"
                " on conflict (content_hash, model) do nothing",
                (content_hash, model, list(vector)),
            )

    def get_retrieval(self, content_hash: str, index_hash: str, policy_hash: str) -> list | None:
        with self._connect() as conn:
            row = _row(
                conn,
                "select candidates from retrievals"
                " where content_hash = %s and index_hash = %s and policy_hash = %s",
                (content_hash, index_hash, policy_hash),
            )
        return row["candidates"] if row else None

    def put_retrieval(
        self, content_hash: str, index_hash: str, policy_hash: str, candidates: list
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into retrievals (content_hash, index_hash, policy_hash, candidates)"
                " values (%s, %s, %s, %s)"
                " on conflict (content_hash, index_hash, policy_hash) do nothing",
                (content_hash, index_hash, policy_hash, Jsonb(candidates)),
            )


_REVIEW_COLUMNS = (
    "review_id, document_id, agent_version, scope, status, counts, cost_usd, trace_id, error,"
    " created_at, started_at, finished_at"
)


def _review(row: dict | None) -> Review | None:
    if row is None:
        return None
    return Review(**{**row, "counts": ReviewCounts(**row["counts"])})


class PostgresReviewRepo(_Repo):
    def create(self, review: Review) -> Review:
        with self._connect() as conn:
            conn.execute(
                "insert into reviews (review_id, document_id, agent_version, scope, status,"
                " counts, cost_usd, trace_id, error, created_at, started_at, finished_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()),"
                " %s, %s) on conflict do nothing",
                (
                    review.review_id,
                    review.document_id,
                    review.agent_version,
                    list(review.scope),
                    review.status,
                    Jsonb(review.counts.model_dump()),
                    review.cost_usd,
                    review.trace_id,
                    review.error,
                    review.created_at,
                    review.started_at,
                    review.finished_at,
                ),
            )
            row = _row(
                conn,
                f"select {_REVIEW_COLUMNS} from reviews where review_id = %s",
                (review.review_id,),
            )
            if row is None:
                # Lost to reviews_one_active_idx: another active review of this
                # document and version exists, and it is the one to use.
                row = _row(
                    conn,
                    f"select {_REVIEW_COLUMNS} from reviews where document_id = %s"
                    " and agent_version = %s and status in ('queued', 'running')",
                    (review.document_id, review.agent_version),
                )
        return _review(row)

    def _one(self, where: str, params: tuple) -> Review | None:
        with self._connect() as conn:
            row = _row(
                conn,
                f"select {_REVIEW_COLUMNS} from reviews where {where}"
                " order by created_at desc limit 1",
                params,
            )
        return _review(row)

    def get(self, review_id: str) -> Review | None:
        return self._one("review_id = %s", (review_id,))

    def active_for(self, document_id: str, agent_version: str) -> Review | None:
        return self._one(
            "document_id = %s and agent_version = %s and status in ('queued', 'running')",
            (document_id, agent_version),
        )

    def latest_for(self, document_id: str) -> Review | None:
        return self._one("document_id = %s", (document_id,))

    def running(self) -> list[Review]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {_REVIEW_COLUMNS} from reviews where status = 'running'"
                " order by created_at",
            )
        return [_review(r) for r in rows]

    def set_status(self, review_id: str, status: ReviewStatus, error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "update reviews set status = %s, error = %s,"
                " started_at = case when %s::text = 'running'"
                " then coalesce(started_at, clock_timestamp()) else started_at end,"
                " finished_at = case when %s::text in ('finished', 'failed') then clock_timestamp()"
                " else finished_at end"
                " where review_id = %s",
                (status, error, status, status, review_id),
            )

    def update_counts(self, review_id: str, counts: ReviewCounts, cost_usd: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "update reviews set counts = %s, cost_usd = %s where review_id = %s",
                (Jsonb(counts.model_dump()), cost_usd, review_id),
            )

    def set_trace(self, review_id: str, trace_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "update reviews set trace_id = %s where review_id = %s", (trace_id, review_id)
            )

    def set_clause_status(
        self, review_id: str, position: int, status: ClauseStatus, error: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into review_clauses (review_id, position, status, error)"
                " values (%s, %s, %s, %s)"
                " on conflict (review_id, position) do update set"
                " status = excluded.status, error = excluded.error",
                (review_id, position, status, error),
            )

    def clause_statuses(self, review_id: str) -> dict[int, tuple[ClauseStatus, str | None]]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select position, status, error from review_clauses"
                " where review_id = %s order by position",
                (review_id,),
            )
        return {r["position"]: (r["status"], r["error"]) for r in rows}


class PostgresEventRepo(_Repo):
    def append(self, review_id: str, type: str, payload: dict) -> StoredEvent:
        with self._connect() as conn:
            # The lock is per review and released at commit, so concurrent appends to one
            # review queue up and read max(seq) one at a time: gap-free and unique, with
            # other reviews unaffected.
            conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 1))", (review_id,))
            row = _row(
                conn,
                "insert into run_events (review_id, seq, type, payload)"
                " select %s::text, coalesce(max(seq), 0) + 1, %s::text, %s from run_events"
                " where review_id = %s"
                " returning review_id, seq, type, payload, created_at",
                (review_id, type, Jsonb(payload), review_id),
            )
        return StoredEvent(**row)

    def since(self, review_id: str, after_seq: int = 0) -> Iterator[StoredEvent]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select review_id, seq, type, payload, created_at from run_events"
                " where review_id = %s and seq > %s order by seq",
                (review_id, after_seq),
            )
        return iter([StoredEvent(**r) for r in rows])


_DECISION_COLUMNS = (
    "review_id, position, action, corrected_verdict, corrected_basis, note, decided_by, decided_at"
)


class PostgresDecisionRepo(_Repo):
    def put(self, decision: Decision) -> Decision:
        with self._connect() as conn:
            row = _row(
                conn,
                f"insert into decisions ({_DECISION_COLUMNS})"
                " values (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()))"
                " on conflict (review_id, position) do update set"
                " action = excluded.action, corrected_verdict = excluded.corrected_verdict,"
                " corrected_basis = excluded.corrected_basis, note = excluded.note,"
                " decided_by = excluded.decided_by, decided_at = excluded.decided_at"
                f" returning {_DECISION_COLUMNS}",
                (
                    decision.review_id,
                    decision.position,
                    decision.action,
                    decision.corrected_verdict,
                    decision.corrected_basis,
                    decision.note,
                    decision.decided_by,
                    decision.decided_at,
                ),
            )
        return Decision(**row)

    def for_review(self, review_id: str) -> dict[int, Decision]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {_DECISION_COLUMNS} from decisions where review_id = %s order by position",
                (review_id,),
            )
        return {r["position"]: Decision(**r) for r in rows}


_FEEDBACK_COLUMNS = (
    "feedback_id, review_id, position, agent_version, vote, reasons, corrected_verdict,"
    " corrected_basis, comment, source, created_at, observations"
)


class PostgresFeedbackRepo(_Repo):
    def put(self, feedback: Feedback) -> Feedback:
        step = next((REASON_STEP[r] for r in feedback.reasons if REASON_STEP[r]), None)
        if feedback.vote == "up":
            step = "clause_span"  # a thumbs-up is scored on the clause span
        with self._connect() as conn:
            row = _row(
                conn,
                f"insert into feedback ({_FEEDBACK_COLUMNS}, step)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " coalesce(%s, clock_timestamp()), %s, %s)"
                " on conflict (feedback_id) do update set"
                " agent_version = excluded.agent_version, vote = excluded.vote,"
                " reasons = excluded.reasons, corrected_verdict = excluded.corrected_verdict,"
                " corrected_basis = excluded.corrected_basis, comment = excluded.comment,"
                " source = excluded.source, step = excluded.step,"
                " created_at = excluded.created_at, observations = excluded.observations"
                f" returning {_FEEDBACK_COLUMNS}",
                (
                    feedback.feedback_id,
                    feedback.review_id,
                    feedback.position,
                    feedback.agent_version,
                    feedback.vote,
                    feedback.reasons,
                    feedback.corrected_verdict,
                    feedback.corrected_basis,
                    feedback.comment,
                    feedback.source,
                    feedback.created_at,
                    Jsonb(feedback.observations),
                    step,
                ),
            )
        return Feedback(**row)

    def for_review(self, review_id: str) -> dict[int, Feedback]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select distinct on (position) {_FEEDBACK_COLUMNS} from feedback"
                " where review_id = %s order by position, created_at desc",
                (review_id,),
            )
        return {r["position"]: Feedback(**r) for r in rows}


@dataclass
class PostgresStore:
    """Satisfies `app.store.interfaces.Store`."""

    files: FileStore
    documents: PostgresDocumentRepo
    verdicts: PostgresVerdictRepo
    caches: PostgresCacheRepo
    reviews: PostgresReviewRepo
    events: PostgresEventRepo
    decisions: PostgresDecisionRepo
    feedback: PostgresFeedbackRepo


def postgres_store(files: FileStore | None = None, connect: Connect = connection) -> PostgresStore:
    """A store over Postgres. `connect` is injectable so tests can point at a Neon branch."""
    return PostgresStore(
        files=files or LocalFileStore(),
        documents=PostgresDocumentRepo(connect),
        verdicts=PostgresVerdictRepo(connect),
        caches=PostgresCacheRepo(connect),
        reviews=PostgresReviewRepo(connect),
        events=PostgresEventRepo(connect),
        decisions=PostgresDecisionRepo(connect),
        feedback=PostgresFeedbackRepo(connect),
    )

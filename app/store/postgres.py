"""Postgres implementations of the store protocols, with pooled production connections.

Each method is one short transaction on a checked-out connection, which keeps
repositories thread-safe while avoiding a Neon handshake for every query.
Content-addressed writes are `insert ... on conflict do nothing` followed by a
select, so the first writer wins and every caller gets the stored row back.
Array columns hold lists of ids; rects, observations and payloads are JSONB.
"""

import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from core.db import connection
from core.settings import load_settings
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import (
    DEFAULT_OWNER_ID,
    IDLE_REVIEW_STATUSES,
    REASON_STEP,
    Agreement,
    AgreementByVerdict,
    AgreementCounts,
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    Decision,
    DecisionEvent,
    Document,
    Feedback,
    ObligationCode,
    Review,
    ReviewCounts,
    ReviewStatus,
    StepObservations,
    StoredEvent,
    agreement_rate,
    decided_verdict,
)
from app.store.deletion import CONTENT_LOCK, PostgresDeletionRepo
from app.store.files import LocalFileStore
from app.store.interfaces import FileStore

Connect = Callable[[], AbstractContextManager[Any]]


IDLE_CHECK_SECONDS = 30.0


class _ConnectionPool:
    """Small lazy pool used because psycopg-pool is not an installed dependency."""

    def __init__(self, max_size: int = 8) -> None:
        self._max_size = max_size
        # (connection, monotonic time it was returned), most recently used first.
        self._idle: queue.LifoQueue[tuple[Any, float]] = queue.LifoQueue()
        self._created = 0
        self._closed = False
        self._lock = threading.Lock()

    def _new(self) -> Any:
        url = load_settings().database_url
        if not url:
            raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")
        return psycopg.connect(url)

    def _checkout(self) -> Any:
        while True:
            try:
                conn, since = self._idle.get_nowait()
            except queue.Empty:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("the Postgres connection pool is closed") from None
                    if self._created < self._max_size:
                        self._created += 1
                        create = True
                    else:
                        create = False
                if create:
                    try:
                        return self._new()
                    except Exception:
                        with self._lock:
                            self._created -= 1
                        raise
                conn, since = self._idle.get()
            if not conn.closed and self._alive(conn, since):
                return conn
            self._discard(conn)

    @staticmethod
    def _alive(conn: Any, since: float) -> bool:
        """Neon drops idle connections server-side, and `closed` stays False until the next
        use, so a connection idle for a while is pinged before it is handed out."""
        if time.monotonic() - since < IDLE_CHECK_SECONDS:
            return True
        try:
            conn.execute("select 1")
            conn.rollback()
            return True
        except Exception:  # noqa: BLE001 - any failure means: replace it
            return False

    def _discard(self, conn: Any) -> None:
        try:
            conn.close()
        finally:
            with self._lock:
                self._created -= 1

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._checkout()
        discard = False
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                discard = True
            raise
        finally:
            with self._lock:
                closed = self._closed
            if discard or closed or conn.closed:
                self._discard(conn)
            else:
                self._idle.put((conn, time.monotonic()))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                conn, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            self._discard(conn)


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
    def record_upload(
        self,
        document_id: str,
        filename: str,
        source_type: str,
        size: int,
        document_type: str = "agb",
        *,
        owner_id: str = DEFAULT_OWNER_ID,
    ) -> str:
        upload_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "insert into uploads"
                " (upload_id, document_id, filename, source_type, size, document_type,"
                " owner_id) values (%s, %s, %s, %s, %s, %s, %s)",
                (upload_id, document_id, filename, source_type, size, document_type, owner_id),
            )
        return upload_id

    @staticmethod
    def _document(row: dict) -> Document:
        return Document(**row)

    _DOC_COLUMNS = (
        "document_id, filename, source_type, page_count, parser_version, splitter_version,"
        " text_hash, document_type, created_at, owner_id, source_hash"
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
            # Shared side of the deletion lock (9.B): saves run side by side, but a deletion
            # or orphan sweep (exclusive) either sees this document's clauses committed or
            # runs before any of its rows exist, never between its clause texts and clauses.
            conn.execute("select pg_advisory_xact_lock_shared(%s)", (CONTENT_LOCK,))
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
            # A new parser or splitter version replaces the earlier parse of the same bytes;
            # the owner and the source hash are part of the id and never change.
            conn.execute(
                "insert into documents (document_id, filename, source_type, page_count,"
                " parser_version, splitter_version, text_hash, created_at, owner_id,"
                " source_hash, document_type)"
                " values (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()),"
                " %s, %s, %s)"
                " on conflict (document_id) do update set"
                " filename = excluded.filename, source_type = excluded.source_type,"
                " page_count = excluded.page_count, parser_version = excluded.parser_version,"
                " splitter_version = excluded.splitter_version, text_hash = excluded.text_hash,"
                " document_type = excluded.document_type",
                (
                    document.document_id,
                    document.filename,
                    document.source_type,
                    document.page_count,
                    document.parser_version,
                    document.splitter_version,
                    document.text_hash,
                    document.created_at,
                    document.owner_id,
                    document.source_hash or document.document_id,
                    document.document_type,
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

    def list(self, limit: int = 100, *, owner_id: str | None = None) -> list[Document]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {self._DOC_COLUMNS} from documents"
                " where %s::text is null or owner_id = %s::text"
                " order by created_at desc limit %s",
                (owner_id, owner_id, limit),
            )
        return [self._document(r) for r in rows]


_VERDICT_COLUMNS = (
    "content_hash, agent_version, verdict, legal_basis, candidates, basis_scores, reason_de,"
    " reason_unavailable, confidence, void_probability, check_rule, trace_id, observations,"
    " input_tokens, output_tokens, latency_ms, obligation_findings"
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

    def get_many(self, content_hashes: list[str], agent_version: str) -> dict[str, ClauseVerdict]:
        hashes = list(dict.fromkeys(content_hashes))
        if not hashes:
            return {}
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {_VERDICT_COLUMNS} from verdicts"
                " where content_hash = any(%s) and agent_version = %s",
                (hashes, agent_version),
            )
        verdicts = [_verdict(row) for row in rows]
        return {verdict.content_hash: verdict for verdict in verdicts}

    def save(self, verdict: ClauseVerdict) -> ClauseVerdict:
        with self._connect() as conn:
            conn.execute(
                f"insert into verdicts ({_VERDICT_COLUMNS})"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s)"
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
                    Jsonb([item.model_dump(mode="json") for item in verdict.obligation_findings]),
                ),
            )
            row = _row(
                conn,
                f"select {_VERDICT_COLUMNS} from verdicts"
                " where content_hash = %s and agent_version = %s",
                (verdict.content_hash, verdict.agent_version),
            )
        return _verdict(row)

    def set_reason(
        self,
        content_hash: str,
        agent_version: str,
        reason_de: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ClauseVerdict | None:
        """Fill in a missing reasoning; None if the verdict is gone or already has one."""
        with self._connect() as conn:
            row = _row(
                conn,
                "update verdicts set reason_de = %s, reason_unavailable = false,"
                " input_tokens = input_tokens + %s, output_tokens = output_tokens + %s"
                " where content_hash = %s and agent_version = %s and reason_unavailable"
                f" returning {_VERDICT_COLUMNS}",
                (reason_de, input_tokens, output_tokens, content_hash, agent_version),
            )
        return _verdict(row) if row else None


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

    def get_topics(self, content_hashes: list[str], model: str) -> dict[str, str]:
        hashes = list(dict.fromkeys(content_hashes))
        if not hashes:
            return {}
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select content_hash, topic from topics where content_hash = any(%s)"
                " and model = %s",
                (hashes, model),
            )
        return {r["content_hash"]: r["topic"] for r in rows}

    def put_topics(self, model: str, topics: dict[str, str]) -> None:
        if not topics:
            return
        hashes = list(topics)
        with self._connect() as conn:
            conn.execute(
                "insert into topics (content_hash, model, topic)"
                " select h, %s::text, t from unnest(%s::text[], %s::text[]) as u(h, t)"
                " on conflict (content_hash, model) do nothing",
                (model, hashes, [topics[h] for h in hashes]),
            )

    def get_obligations(
        self, content_hashes: list[str], model: str, profile: str
    ) -> dict[str, list[ObligationCode]]:
        hashes = list(dict.fromkeys(content_hashes))
        if not hashes:
            return {}
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select content_hash, obligations from obligation_routes"
                " where content_hash = any(%s) and model = %s and profile = %s",
                (hashes, model, profile),
            )
        return {row["content_hash"]: list(row["obligations"]) for row in rows}

    def put_obligations(
        self,
        model: str,
        profile: str,
        obligations: dict[str, list[ObligationCode]],
    ) -> None:
        if not obligations:
            return
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "insert into obligation_routes (content_hash, model, profile, obligations)"
                " values (%s, %s, %s, %s)"
                " on conflict (content_hash, model, profile) do nothing",
                [
                    (content_hash, model, profile, found)
                    for content_hash, found in obligations.items()
                ],
            )

    def get_summary_text(self, decision_set_hash: str, model: str) -> str | None:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select text from summary_texts where decision_set_hash = %s and model = %s",
                (decision_set_hash, model),
            )
        return rows[0]["text"] if rows else None

    def put_summary_text(
        self, decision_set_hash: str, model: str, text: str, *, review_id: str | None = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "insert into summary_texts (decision_set_hash, model, text, review_id)"
                " values (%s, %s, %s, %s) on conflict (decision_set_hash, model) do nothing",
                (decision_set_hash, model, text, review_id),
            )


_REVIEW_COLUMNS = (
    "review_id, document_id, agent_version, scope, status, counts, cost_usd, trace_id, error,"
    " created_at, started_at, finished_at, owner_id, document_type, obligations"
)


def _review(row: dict | None) -> Review | None:
    if row is None:
        return None
    return Review(**{**row, "counts": ReviewCounts(**row["counts"])})


class PostgresReviewRepo(_Repo):
    def create(self, review: Review) -> Review:
        with self._connect() as conn:
            # A deletion holds the document row `for update`: wait for it, and refuse a
            # review of a document it just deleted (its tombstone says so) rather than
            # leave a queued review with no document behind.
            document = _row(
                conn,
                "select 1 as found from documents where document_id = %s for share",
                (review.document_id,),
            )
            if document is None and _row(
                conn,
                "select 1 as found from deletion_tombstones"
                " where kind = 'document' and subject_id = %s limit 1",
                (review.document_id,),
            ):
                raise KeyError(review.document_id)
            conn.execute(
                "insert into reviews (review_id, document_id, agent_version, scope, status,"
                " counts, cost_usd, trace_id, error, created_at, started_at, finished_at,"
                " owner_id, document_type, obligations)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()),"
                " %s, %s, %s, %s, %s) on conflict do nothing",
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
                    review.owner_id,
                    review.document_type,
                    list(review.obligations),
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
                # The end of the last run: moving between idle statuses (a decision settling
                # awaiting_decisions → finished) keeps it. The right side reads old values.
                " finished_at = case when %s::text = any(%s::text[])"
                " and not (status = any(%s::text[])) then clock_timestamp()"
                " else finished_at end"
                " where review_id = %s",
                (
                    status,
                    error,
                    status,
                    status,
                    list(IDLE_REVIEW_STATUSES),
                    list(IDLE_REVIEW_STATUSES),
                    review_id,
                ),
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

    def set_clause_statuses(
        self, review_id: str, statuses: dict[int, tuple[ClauseStatus, str | None]]
    ) -> None:
        if not statuses:
            return
        positions = list(statuses)
        with self._connect() as conn:
            conn.execute(
                "insert into review_clauses (review_id, position, status, error)"
                " select %s::text, p, s, e from unnest(%s::int[], %s::text[], %s::text[])"
                " as t(p, s, e)"
                " on conflict (review_id, position) do update set"
                " status = excluded.status, error = excluded.error",
                (
                    review_id,
                    positions,
                    [statuses[p][0] for p in positions],
                    [statuses[p][1] for p in positions],
                ),
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

    def set_clause_topics(self, review_id: str, topics: dict[int, str]) -> None:
        if not topics:
            return
        positions = list(topics)
        with self._connect() as conn:
            conn.execute(
                "insert into review_clauses (review_id, position, status, topic)"
                " select %s::text, p, 'queued', t from unnest(%s::int[], %s::text[]) as u(p, t)"
                " on conflict (review_id, position) do update set topic = excluded.topic",
                (review_id, positions, [topics[p] for p in positions]),
            )

    def clause_topics(self, review_id: str) -> dict[int, str]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select position, topic from review_clauses"
                " where review_id = %s and topic is not null order by position",
                (review_id,),
            )
        return {r["position"]: r["topic"] for r in rows}

    def set_clause_obligations(
        self, review_id: str, obligations: dict[int, list[ObligationCode]]
    ) -> None:
        if not obligations:
            return
        positions = list(obligations)
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                "insert into review_clauses (review_id, position, status, obligations)"
                " values (%s, %s, 'queued', %s)"
                " on conflict (review_id, position) do update set"
                " obligations = excluded.obligations",
                [(review_id, position, obligations[position]) for position in positions],
            )

    def clause_obligations(self, review_id: str) -> dict[int, list[ObligationCode]]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select position, obligations from review_clauses"
                " where review_id = %s and cardinality(obligations) > 0 order by position",
                (review_id,),
            )
        return {row["position"]: list(row["obligations"]) for row in rows}


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

    def append_many(self, review_id: str, events: list[tuple[str, dict]]) -> list[StoredEvent]:
        if not events:
            return []
        items = [{"type": type, "payload": payload} for type, payload in events]
        with self._connect() as conn:
            # Same per-review lock as `append`, held to commit: the batch gets consecutive
            # seqs and no concurrent append lands inside it.
            conn.execute("select pg_advisory_xact_lock(hashtextextended(%s, 1))", (review_id,))
            rows = _rows(
                conn,
                "insert into run_events (review_id, seq, type, payload)"
                " select %s::text, base.top + t.ord, t.el ->> 'type', t.el -> 'payload'"
                " from (select coalesce(max(seq), 0) as top from run_events"
                " where review_id = %s) as base,"
                " jsonb_array_elements(%s) with ordinality as t(el, ord)"
                " returning review_id, seq, type, payload, created_at",
                (review_id, review_id, Jsonb(items)),
            )
        return sorted((StoredEvent(**r) for r in rows), key=lambda e: e.seq)

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
    "review_id, position, action, corrected_verdict, corrected_basis, note, decided_by,"
    " decided_at, model_verdict, model_basis, reason_de, reason_unavailable"
)
_DECISION_EVENT_COLUMNS = (
    "event_id, review_id, position, action, previous_action, from_verdict, from_basis,"
    " to_verdict, to_basis, note, decided_by, authenticated, agent_version, request_id, at"
)


class PostgresDecisionRepo(_Repo):
    def put(
        self,
        decision: Decision,
        *,
        agent_version: str,
        authenticated: bool = False,
        request_id: str | None = None,
    ) -> Decision:
        if decision.model_verdict is None:
            raise ValueError("a decision needs the model's verdict")
        with self._connect() as conn:
            # One writer per clause at a time, so `from_*` is always the row this replaces.
            conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 2))",
                (f"{decision.review_id}:{decision.position}",),
            )
            previous = _row(
                conn,
                f"select {_DECISION_COLUMNS} from decisions where review_id = %s and position = %s",
                (decision.review_id, decision.position),
            )
            row = _row(
                conn,
                f"insert into decisions ({_DECISION_COLUMNS})"
                " values (%s, %s, %s, %s, %s, %s, %s, coalesce(%s, clock_timestamp()),"
                " %s, %s, %s, %s)"
                " on conflict (review_id, position) do update set"
                " action = excluded.action, corrected_verdict = excluded.corrected_verdict,"
                " corrected_basis = excluded.corrected_basis, note = excluded.note,"
                " decided_by = excluded.decided_by, decided_at = excluded.decided_at,"
                " model_verdict = excluded.model_verdict, model_basis = excluded.model_basis,"
                " reason_de = excluded.reason_de,"
                " reason_unavailable = excluded.reason_unavailable"
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
                    decision.model_verdict,
                    decision.model_basis,
                    decision.reason_de,
                    decision.reason_unavailable,
                ),
            )
            stored = Decision(**row)
            if previous is None:
                from_verdict, from_basis = stored.model_verdict, list(stored.model_basis)
            else:
                from_verdict, from_basis = decided_verdict(Decision(**previous))
            to_verdict, to_basis = decided_verdict(stored)
            conn.execute(
                "insert into decision_events (review_id, position, action, previous_action,"
                " from_verdict, from_basis, to_verdict, to_basis, note, decided_by,"
                " authenticated, agent_version, request_id, at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    stored.review_id,
                    stored.position,
                    stored.action,
                    previous["action"] if previous else None,
                    from_verdict,
                    from_basis,
                    to_verdict,
                    to_basis,
                    stored.note,
                    stored.decided_by,
                    authenticated,
                    agent_version,
                    request_id,
                    stored.decided_at,
                ),
            )
        return stored

    def history(self, review_id: str, position: int | None = None) -> list[DecisionEvent]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {_DECISION_EVENT_COLUMNS} from decision_events where review_id = %s"
                " and (%s::int is null or position = %s::int) order by event_id",
                (review_id, position, position),
            )
        return [DecisionEvent(**r) for r in rows]

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

    def get(self, feedback_id: str) -> Feedback | None:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select {_FEEDBACK_COLUMNS} from feedback where feedback_id = %s",
                (feedback_id,),
            )
        return Feedback(**rows[0]) if rows else None

    def delete(self, feedback_id: str) -> None:
        """Remove feedback whose source decision was superseded by a dismissal."""
        with self._connect() as conn:
            conn.execute("delete from feedback where feedback_id = %s", (feedback_id,))

    def for_review(self, review_id: str) -> dict[int, Feedback]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                f"select distinct on (position) {_FEEDBACK_COLUMNS} from feedback"
                " where review_id = %s order by position, created_at desc",
                (review_id,),
            )
        return {r["position"]: Feedback(**r) for r in rows}


class PostgresMetricsRepo(_Repo):
    def agreement(self, agent_version: str | None = None) -> list[Agreement]:
        with self._connect() as conn:
            rows = _rows(
                conn,
                "select agent_version, model_verdict, decided, kept, overridden, dismissed,"
                " last_decided_at from decision_agreement"
                " where %s::text is null or agent_version = %s::text",
                (agent_version, agent_version),
            )
        tallies: dict[str, Agreement] = {}
        for row in rows:
            total = tallies.setdefault(
                row["agent_version"], Agreement(agent_version=row["agent_version"])
            )
            per_class = AgreementCounts(
                decided=row["decided"],
                kept=row["kept"],
                overridden=row["overridden"],
                dismissed=row["dismissed"],
                rate=agreement_rate(row["kept"], row["overridden"]),
            )
            if row["model_verdict"] in AgreementByVerdict.model_fields:
                setattr(total.by_verdict, row["model_verdict"], per_class)
            total.decided += per_class.decided
            total.kept += per_class.kept
            total.overridden += per_class.overridden
            total.dismissed += per_class.dismissed
            if total.last_decided_at is None or row["last_decided_at"] > total.last_decided_at:
                total.last_decided_at = row["last_decided_at"]
        for total in tallies.values():
            total.rate = agreement_rate(total.kept, total.overridden)
        return sorted(tallies.values(), key=lambda a: a.last_decided_at, reverse=True)


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
    metrics: PostgresMetricsRepo
    deletion: PostgresDeletionRepo
    _pool: _ConnectionPool | None = None

    def close(self) -> None:
        """Close idle production connections; checked-out ones close when returned."""
        if self._pool is not None:
            self._pool.close()


def postgres_store(files: FileStore | None = None, connect: Connect | None = None) -> PostgresStore:
    """A pooled store in production; `connect` keeps tests independently injectable."""
    pool = None
    if connect is None:
        pool = _ConnectionPool()
        connect = pool.connection
    return PostgresStore(
        files=files or LocalFileStore(),
        documents=PostgresDocumentRepo(connect),
        verdicts=PostgresVerdictRepo(connect),
        caches=PostgresCacheRepo(connect),
        reviews=PostgresReviewRepo(connect),
        events=PostgresEventRepo(connect),
        decisions=PostgresDecisionRepo(connect),
        feedback=PostgresFeedbackRepo(connect),
        metrics=PostgresMetricsRepo(connect),
        deletion=PostgresDeletionRepo(connect),
        _pool=pool,
    )

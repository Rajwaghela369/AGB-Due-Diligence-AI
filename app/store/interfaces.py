"""Repository protocols. Postgres implements them for the service, in-memory fakes for tests.

Every content-addressed write is insert-or-reuse: writing a row whose key exists
returns the stored row and changes nothing. Nothing here expires by time; a new
parser, splitter or agent version is a new key.

All methods are synchronous. The runner calls them from worker threads and the
API from FastAPI's threadpool, so implementations must be thread-safe.
"""

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Protocol

from app.domain import (
    Agreement,
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    DataClass,
    Decision,
    DecisionEvent,
    DeletionReason,
    DeletionReport,
    Document,
    ErasureTarget,
    ErasureTask,
    Feedback,
    Principal,
    Review,
    ReviewCounts,
    ReviewStatus,
    StoredEvent,
    Tombstone,
)


class FileStore(Protocol):
    """Immutable blobs addressed by key: local disk (`app.store.files.LocalFileStore`) or
    S3-compatible EU object storage (`app.store.s3.S3FileStore`, Phase 9, 9.D), chosen by
    `files.backend`. docs/Phase 9 Design.md, "Storage", explains the choice.
    """

    def put(self, key: str, data: bytes) -> str:
        """Store `data` under `key`; returns key. A no-op if the key exists: the first
        writer's blob stands (S3: a conditional `If-None-Match: *` put)."""
        ...

    def get(self, key: str) -> bytes:
        """The blob; FileNotFoundError if absent."""
        ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """Remove the blob (Phase 9, 9.B); True if it existed, False if it was already
        gone, so a retried erasure task is harmless. Only the deletion worker calls it,
        after its database transaction committed and no document references the key."""
        ...

    def presign(self, key: str, expires_in: int) -> str | None:
        """A time-limited URL the browser can download the blob from directly, valid for
        `expires_in` seconds; None when the backend cannot sign (local disk), and the API
        then proxies the bytes itself. Does not check that the key exists."""
        ...

    def list_keys(self, prefix: str = "") -> list[tuple[str, datetime]]:
        """Every stored key starting with `prefix`, with its last-modified time (UTC). The
        orphan sweep (9.B) needs the age: ingest writes the blob before the document, so a
        young unreferenced blob may be an upload in flight (S3: ListObjectsV2)."""
        ...


class DocumentRepo(Protocol):
    def record_upload(self, document_id: str, filename: str, source_type: str, size: int) -> str:
        """Insert one `uploads` row; returns upload_id. One row per upload event."""
        ...

    def get(self, document_id: str) -> Document | None: ...

    def find_parsed(
        self, document_id: str, parser_version: str, splitter_version: str
    ) -> Document | None:
        """The parse cache: a document already split under these versions."""
        ...

    def save(self, document: Document, clauses: list[ClauseSpan], texts: dict[str, str]) -> None:
        """Store document, its clause positions, and clause texts by content_hash (reused)."""
        ...

    def clauses(self, document_id: str) -> list[ClauseSpan]: ...

    def list(self, limit: int = 100, *, owner_id: str | None = None) -> list[Document]:
        """Newest first; only `owner_id`'s documents if given (Phase 9, 9.E)."""
        ...


class VerdictRepo(Protocol):
    def get(self, content_hash: str, agent_version: str) -> ClauseVerdict | None: ...

    def get_many(self, content_hashes: list[str], agent_version: str) -> dict[str, ClauseVerdict]:
        """Return the stored verdicts among `content_hashes`, keyed by content hash."""
        ...

    def set_reason(
        self,
        content_hash: str,
        agent_version: str,
        reason_de: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ClauseVerdict | None:
        """Fill a missing reasoning; None if the verdict is absent or already has one."""
        ...

    def save(self, verdict: ClauseVerdict) -> ClauseVerdict:
        """Insert or reuse on (content_hash, agent_version); returns the stored row."""
        ...


class CacheRepo(Protocol):
    """Embedding and retrieval caches, keyed as in the Phase 5 plan (5.A)."""

    def get_embedding(self, content_hash: str, model: str) -> list[float] | None: ...
    def put_embedding(self, content_hash: str, model: str, vector: list[float]) -> None: ...

    def get_retrieval(self, content_hash: str, index_hash: str, policy_hash: str) -> list | None:
        """Ranked candidates as stored (JSON-serialisable dicts of core.retrieval.Retrieved)."""
        ...

    def put_retrieval(
        self, content_hash: str, index_hash: str, policy_hash: str, candidates: list
    ) -> None: ...

    def get_topics(self, content_hashes: list[str], model: str) -> dict[str, str]:
        """Router topics already classified under `model`, keyed by content hash."""
        ...

    def put_topics(self, model: str, topics: dict[str, str]) -> None:
        """Store `{content_hash: topic}`; insert-or-reuse per (content_hash, model)."""
        ...

    def get_summary_text(self, decision_set_hash: str, model: str) -> str | None:
        """The LLM recommendation paragraph generated for this decision set (Phase 8, 8.A)."""
        ...

    def put_summary_text(
        self, decision_set_hash: str, model: str, text: str, *, review_id: str | None = None
    ) -> None:
        """Store a recommendation; insert-or-reuse per (decision_set_hash, model), so two
        concurrent requests for one decision set keep the first text. `review_id` (Phase 9)
        is the review it was generated for, so deleting that review deletes the paragraph."""
        ...


class ReviewRepo(Protocol):
    def create(self, review: Review) -> Review: ...
    def get(self, review_id: str) -> Review | None: ...

    def active_for(self, document_id: str, agent_version: str) -> Review | None:
        """A queued or running review of this document under this version (in-flight dedupe)."""
        ...

    def latest_for(self, document_id: str) -> Review | None: ...
    def running(self) -> list[Review]: ...  # for resume on startup

    def set_status(
        self, review_id: str, status: ReviewStatus, error: str | None = None
    ) -> None: ...

    def update_counts(self, review_id: str, counts: ReviewCounts, cost_usd: float) -> None: ...
    def set_trace(self, review_id: str, trace_id: str) -> None: ...

    def set_clause_status(
        self, review_id: str, position: int, status: ClauseStatus, error: str | None = None
    ) -> None:
        """Per-review clause state (`review_clauses`); the verdict itself lives in VerdictRepo."""
        ...

    def set_clause_statuses(
        self, review_id: str, statuses: dict[int, tuple[ClauseStatus, str | None]]
    ) -> None:
        """`set_clause_status` for many positions in one transaction (a cost that does not
        grow per clause on a remote database). An empty mapping is a no-op."""
        ...

    def clause_statuses(self, review_id: str) -> dict[int, tuple[ClauseStatus, str | None]]: ...

    def set_clause_topics(self, review_id: str, topics: dict[int, str]) -> None:
        """Store the router topic per position on the review's clause rows, in one
        transaction. A row that does not exist yet is created `queued`; an existing row
        keeps its status. An empty mapping is a no-op."""
        ...

    def clause_topics(self, review_id: str) -> dict[int, str]:
        """The topics `set_clause_topics` stored; positions without one are absent."""
        ...


class EventRepo(Protocol):
    def append(self, review_id: str, type: str, payload: dict) -> StoredEvent:
        """Assign the next `seq` for this review atomically and store the event."""
        ...

    def append_many(self, review_id: str, events: list[tuple[str, dict]]) -> list[StoredEvent]:
        """Append `(type, payload)` pairs in order as one transaction: consecutive seqs,
        nobody else's event in between. An empty list is a no-op."""
        ...

    def since(self, review_id: str, after_seq: int = 0) -> Iterator[StoredEvent]: ...


class DecisionRepo(Protocol):
    def put(
        self,
        decision: Decision,
        *,
        agent_version: str,
        authenticated: bool = False,
        request_id: str | None = None,
    ) -> Decision:
        """Upsert the current decision on (review_id, position) and append its
        `DecisionEvent`, in one transaction, serialised per (review_id, position).

        The event's `from_*` and `previous_action` come from the row being replaced (read
        inside the transaction), or from `decision.model_verdict`/`model_basis` for the first
        decision; `to_*` from `decided_verdict(decision)`; `at` equals the stored
        `decided_at` (now when None). Raises ValueError if `decision.model_verdict` is None.
        Returns the stored decision.
        """
        ...

    def for_review(self, review_id: str) -> dict[int, Decision]: ...

    def history(self, review_id: str, position: int | None = None) -> list[DecisionEvent]:
        """The review's decision events (one position's, if given), oldest first."""
        ...


class MetricsRepo(Protocol):
    def agreement(self, agent_version: str | None = None) -> list[Agreement]:
        """Lawyer agreement per agent version over current decisions (the
        `decision_agreement` view), most recently decided version first; only `agent_version`
        if given. A version without decisions is absent. Decisions without a
        `model_verdict` (made before migration 003 and not backfilled) are left out."""
        ...


class FeedbackRepo(Protocol):
    def put(self, feedback: Feedback) -> Feedback: ...  # upsert on feedback_id
    def get(self, feedback_id: str) -> Feedback | None: ...
    def delete(self, feedback_id: str) -> None: ...  # a dismissal supersedes the decision's vote
    def for_review(self, review_id: str) -> dict[int, Feedback]: ...  # latest per position


class DeletionRepo(Protocol):
    """Reference-counted deletion (Phase 9, 9.B) and the retention queries (9.C).

    docs/Phase 9 API.md, "Deletion", is the exact contract; in short: a document's own rows
    go in one transaction; content-addressed rows (clause texts, verdicts, embeddings,
    retrievals, topics) go only when no other document's clauses (nor the evaluation
    `clauses` table) reference their content hash; whatever lives outside the database is
    queued in the same transaction as `ErasureTask`s and finished after the commit. Every
    deletion appends one `Tombstone`. Raises KeyError for an unknown id and ValueError while
    a review of the document is queued or running (`app.domain.ActiveReviewError`).
    """

    def plan_document(self, document_id: str) -> DeletionReport:
        """What `delete_document` would remove now (`dry_run=True`); changes nothing."""
        ...

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        """Delete the document and everything only it used, in one transaction."""
        ...

    def delete_review(
        self, review_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        """Delete one review (events, clause rows, decisions and their events, feedback,
        its trace and checkpoints); the document and the shared caches stay."""
        ...

    def sweep_orphans(
        self,
        *,
        blobs: Iterable[tuple[str, datetime]] = (),
        summaries_before: datetime | None = None,
    ) -> dict[str, int]:
        """Remove content-addressed rows no document references any more (left by a crash
        or by rows written before 9.B), rows of a review that no longer exists (a write that
        lost a race with the deletion), and queue the `blobs` (a `FileStore.list_keys`
        listing) no document's `blob_key` names. Rows and blobs younger than an hour are
        spared: they may belong to an upload in flight. `summaries_before` also removes
        summary paragraphs with no `review_id` (written before Phase 9) older than it.
        Returns the counts by `DELETION_TABLES` key (plus "blobs"); appends a "sweep"
        tombstone when anything went."""
        ...

    def erase_blob(self, key: str, delete: Callable[[str], object]) -> bool:
        """The blob erasure, re-checked at execution time: unless a document's `blob_key`
        names `key` now, call `delete(key)` while holding the lock ingest takes to save a
        document, so no upload can start using the key in between. Returns False when
        the key is referenced again (the task is then done: the blob must stay)."""
        ...

    def expired(self, data_class: DataClass, before: datetime, limit: int = 100) -> list[str]:
        """Ids past retention: "files" → document ids whose last upload is before `before`;
        "reviews" → idle review ids that finished before it; "traces" → Langfuse trace ids
        of reviews and verdicts created before it and not yet erased. Oldest first."""
        ...

    def pending_erasures(self, limit: int = 100, *, due: bool = False) -> list[ErasureTask]:
        """Erasure tasks not done yet, by `next_attempt_at` then id; with `due`, only those
        whose backoff has passed."""
        ...

    def finish_erasure(self, task_id: int, error: str | None = None) -> None:
        """Mark a task done (`error=None`) or count a failed attempt and push its
        `next_attempt_at` back (exponential backoff, `erasure_backoff`)."""
        ...

    def queue_erasures(self, tasks: list[tuple[ErasureTarget, str]]) -> list[ErasureTask]:
        """Queue erasures outside a deletion (expired traces); an already pending
        (target, key) is reused."""
        ...

    def tombstones(self, subject_id: str | None = None) -> list[Tombstone]:
        """The deletion trail, oldest first; one subject's if given."""
        ...


class Store(Protocol):
    """Everything the service needs, bundled so it can be injected as one dependency."""

    files: FileStore
    documents: DocumentRepo
    verdicts: VerdictRepo
    caches: CacheRepo
    reviews: ReviewRepo
    events: EventRepo
    decisions: DecisionRepo
    feedback: FeedbackRepo
    metrics: MetricsRepo
    deletion: DeletionRepo

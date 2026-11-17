"""Repository protocols. Postgres implements them for the service, in-memory fakes for tests.

Every content-addressed write is insert-or-reuse: writing a row whose key exists
returns the stored row and changes nothing. Nothing here expires by time; a new
parser, splitter or agent version is a new key.

All methods are synchronous. The runner calls them from worker threads and the
API from FastAPI's threadpool, so implementations must be thread-safe.
"""

from collections.abc import Iterator
from typing import Protocol

from app.domain import (
    Agreement,
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    Decision,
    DecisionEvent,
    Document,
    Feedback,
    Review,
    ReviewCounts,
    ReviewStatus,
    StoredEvent,
)


class FileStore(Protocol):
    """Immutable blobs addressed by key. Local disk now, EU object storage in Phase 9."""

    def put(self, key: str, data: bytes) -> str: ...  # returns key; no-op if present
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


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

    def list(self, limit: int = 100) -> list[Document]: ...


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
    def for_review(self, review_id: str) -> dict[int, Feedback]: ...  # latest per position


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

"""In-memory fakes of every repository protocol, for tests and for running without a database.

Other work streams test against these, so they follow the Postgres semantics
rather than the easiest thing: content-addressed writes are insert-or-reuse,
`seq` is gap-free per review, reads return copies (a caller mutating a returned
model must not change the "database"), and every method takes one lock so the
fakes are as thread-safe as the real thing.
"""

import threading
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain import (
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Review,
    ReviewCounts,
    ReviewStatus,
    StoredEvent,
)


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryFileStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.RLock()

    def put(self, key: str, data: bytes) -> str:
        with self._lock:
            self._blobs.setdefault(key, bytes(data))
        return key

    def get(self, key: str) -> bytes:
        with self._lock:
            try:
                return self._blobs[key]
            except KeyError:
                raise FileNotFoundError(key) from None

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._blobs


class MemoryDocumentRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.uploads: list[dict] = []
        self._documents: dict[str, Document] = {}
        self._clauses: dict[str, list[ClauseSpan]] = {}
        self._texts: dict[str, str] = {}  # content_hash -> text, stored once

    def record_upload(self, document_id: str, filename: str, source_type: str, size: int) -> str:
        upload_id = uuid.uuid4().hex
        with self._lock:
            self.uploads.append(
                {
                    "upload_id": upload_id,
                    "document_id": document_id,
                    "filename": filename,
                    "source_type": source_type,
                    "size": size,
                    "uploaded_at": _now(),
                }
            )
        return upload_id

    def get(self, document_id: str) -> Document | None:
        with self._lock:
            doc = self._documents.get(document_id)
            return doc.model_copy(deep=True) if doc else None

    def find_parsed(
        self, document_id: str, parser_version: str, splitter_version: str
    ) -> Document | None:
        doc = self.get(document_id)
        if doc and (doc.parser_version, doc.splitter_version) == (parser_version, splitter_version):
            return doc
        return None

    def save(self, document: Document, clauses: list[ClauseSpan], texts: dict[str, str]) -> None:
        with self._lock:
            for clause in clauses:
                self._texts.setdefault(
                    clause.content_hash, texts.get(clause.content_hash, clause.text)
                )
            existing = self._documents.get(document.document_id)
            if (
                existing
                and existing.parser_version == document.parser_version
                and existing.splitter_version == document.splitter_version
            ):
                return  # same bytes under the same versions: reuse
            # A new parser or splitter version replaces the earlier parse of the same bytes.
            stored = document.model_copy(deep=True)
            stored.created_at = (existing.created_at if existing else None) or stored.created_at
            stored.created_at = stored.created_at or _now()
            self._documents[document.document_id] = stored
            self._clauses[document.document_id] = sorted(
                (c.model_copy(deep=True) for c in clauses), key=lambda c: c.position
            )

    def clauses(self, document_id: str) -> list[ClauseSpan]:
        with self._lock:
            out = []
            for c in self._clauses.get(document_id, []):
                copy = c.model_copy(deep=True)
                copy.text = self._texts.get(c.content_hash, c.text)
                out.append(copy)
            return out

    def list(self, limit: int = 100) -> list[Document]:
        with self._lock:
            docs = sorted(self._documents.values(), key=lambda d: d.created_at, reverse=True)
            return [d.model_copy(deep=True) for d in docs[:limit]]

    def text_count(self) -> int:
        """Rows in `clause_texts`; lets tests assert a repeated clause is stored once."""
        with self._lock:
            return len(self._texts)


class MemoryVerdictRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], ClauseVerdict] = {}

    def get(self, content_hash: str, agent_version: str) -> ClauseVerdict | None:
        with self._lock:
            row = self._rows.get((content_hash, agent_version))
            return row.model_copy(deep=True) if row else None

    def get_many(self, content_hashes: list[str], agent_version: str) -> dict[str, ClauseVerdict]:
        with self._lock:
            return {
                content_hash: row.model_copy(deep=True)
                for content_hash in dict.fromkeys(content_hashes)
                if (row := self._rows.get((content_hash, agent_version))) is not None
            }

    def save(self, verdict: ClauseVerdict) -> ClauseVerdict:
        with self._lock:
            row = self._rows.setdefault(
                (verdict.content_hash, verdict.agent_version), verdict.model_copy(deep=True)
            )
            return row.model_copy(deep=True)

    def set_reason(
        self,
        content_hash: str,
        agent_version: str,
        reason_de: str,
        input_tokens: int,
        output_tokens: int,
    ) -> ClauseVerdict | None:
        """Fill in a missing reasoning; None if the verdict is gone or already has one."""
        with self._lock:
            row = self._rows.get((content_hash, agent_version))
            if row is None or not row.reason_unavailable:
                return None
            row.reason_de, row.reason_unavailable = reason_de, False
            row.input_tokens += input_tokens
            row.output_tokens += output_tokens
            return row.model_copy(deep=True)


class MemoryCacheRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._embeddings: dict[tuple[str, str], list[float]] = {}
        self._retrievals: dict[tuple[str, str, str], list] = {}

    def get_embedding(self, content_hash: str, model: str) -> list[float] | None:
        with self._lock:
            vec = self._embeddings.get((content_hash, model))
            return list(vec) if vec is not None else None

    def put_embedding(self, content_hash: str, model: str, vector: list[float]) -> None:
        with self._lock:
            self._embeddings.setdefault((content_hash, model), list(vector))

    def get_retrieval(self, content_hash: str, index_hash: str, policy_hash: str) -> list | None:
        with self._lock:
            found = self._retrievals.get((content_hash, index_hash, policy_hash))
            return deepcopy(found) if found is not None else None

    def put_retrieval(
        self, content_hash: str, index_hash: str, policy_hash: str, candidates: list
    ) -> None:
        with self._lock:
            self._retrievals.setdefault(
                (content_hash, index_hash, policy_hash), deepcopy(candidates)
            )


class MemoryReviewRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reviews: dict[str, Review] = {}
        self._order: dict[str, int] = {}  # insertion order breaks created_at ties
        self._clauses: dict[str, dict[int, tuple[ClauseStatus, str | None]]] = {}

    def create(self, review: Review) -> Review:
        with self._lock:
            existing = self._reviews.get(review.review_id)
            if existing is not None:
                return existing.model_copy(deep=True)
            if review.status in ("queued", "running"):
                # Postgres: reviews_one_active_idx; the active review wins.
                active = self._active(review.document_id, review.agent_version)
                if active is not None:
                    return active.model_copy(deep=True)
            stored = review.model_copy(deep=True)
            stored.created_at = stored.created_at or _now()
            self._reviews[review.review_id] = stored
            self._order[review.review_id] = len(self._order)
            return stored.model_copy(deep=True)

    def _copy(self, review: Review | None) -> Review | None:
        return review.model_copy(deep=True) if review else None

    def get(self, review_id: str) -> Review | None:
        with self._lock:
            return self._copy(self._reviews.get(review_id))

    def _newest(self, reviews: list[Review]) -> Review | None:
        if not reviews:
            return None
        return max(reviews, key=lambda r: (r.created_at, self._order[r.review_id]))

    def active_for(self, document_id: str, agent_version: str) -> Review | None:
        with self._lock:
            return self._copy(
                self._newest(
                    [
                        r
                        for r in self._reviews.values()
                        if r.document_id == document_id
                        and r.agent_version == agent_version
                        and r.status in ("queued", "running")
                    ]
                )
            )

    def latest_for(self, document_id: str) -> Review | None:
        with self._lock:
            return self._copy(
                self._newest([r for r in self._reviews.values() if r.document_id == document_id])
            )

    def running(self) -> list[Review]:
        with self._lock:
            rows = [r for r in self._reviews.values() if r.status == "running"]
            return [r.model_copy(deep=True) for r in sorted(rows, key=lambda r: r.created_at)]

    def _active(self, document_id: str, agent_version: str) -> Review | None:
        for r in self._reviews.values():
            if (
                r.document_id == document_id
                and r.agent_version == agent_version
                and r.status in ("queued", "running")
            ):
                return r
        return None

    def set_status(self, review_id: str, status: ReviewStatus, error: str | None = None) -> None:
        with self._lock:
            review = self._reviews[review_id]
            if status in ("queued", "running"):
                active = self._active(review.document_id, review.agent_version)
                if active is not None and active.review_id != review_id:
                    # Postgres: unique violation on reviews_one_active_idx.
                    raise ValueError(f"review {active.review_id} is already active")
            review.status = status
            review.error = error
            if status == "running" and review.started_at is None:
                review.started_at = _now()
            if status in ("finished", "failed"):
                review.finished_at = _now()

    def update_counts(self, review_id: str, counts: ReviewCounts, cost_usd: float) -> None:
        with self._lock:
            review = self._reviews[review_id]
            review.counts = counts.model_copy(deep=True)
            review.cost_usd = cost_usd

    def set_trace(self, review_id: str, trace_id: str) -> None:
        with self._lock:
            self._reviews[review_id].trace_id = trace_id

    def set_clause_status(
        self, review_id: str, position: int, status: ClauseStatus, error: str | None = None
    ) -> None:
        with self._lock:
            if review_id not in self._reviews:
                raise KeyError(review_id)  # Postgres: foreign key violation
            self._clauses.setdefault(review_id, {})[position] = (status, error)

    def set_clause_statuses(
        self, review_id: str, statuses: dict[int, tuple[ClauseStatus, str | None]]
    ) -> None:
        if not statuses:
            return
        with self._lock:
            if review_id not in self._reviews:
                raise KeyError(review_id)  # Postgres: foreign key violation
            self._clauses.setdefault(review_id, {}).update(statuses)

    def clause_statuses(self, review_id: str) -> dict[int, tuple[ClauseStatus, str | None]]:
        with self._lock:
            return dict(sorted(self._clauses.get(review_id, {}).items()))


class MemoryEventRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: dict[str, list[StoredEvent]] = {}

    def append(self, review_id: str, type: str, payload: dict) -> StoredEvent:
        with self._lock:
            log = self._events.setdefault(review_id, [])
            event = StoredEvent(
                review_id=review_id,
                seq=len(log) + 1,
                type=type,  # type: ignore[arg-type]
                payload=deepcopy(payload),
                created_at=_now(),
            )
            log.append(event)
            return event.model_copy(deep=True)

    def append_many(self, review_id: str, events: list[tuple[str, dict]]) -> list[StoredEvent]:
        with self._lock:
            return [self.append(review_id, type, payload) for type, payload in events]

    def since(self, review_id: str, after_seq: int = 0) -> Iterator[StoredEvent]:
        with self._lock:
            events = [e.model_copy(deep=True) for e in self._events.get(review_id, [])]
        return iter([e for e in events if e.seq > after_seq])


class MemoryDecisionRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, int], Decision] = {}

    def put(self, decision: Decision) -> Decision:
        with self._lock:
            stored = decision.model_copy(deep=True)
            stored.decided_at = stored.decided_at or _now()
            self._rows[(decision.review_id, decision.position)] = stored
            return stored.model_copy(deep=True)

    def for_review(self, review_id: str) -> dict[int, Decision]:
        with self._lock:
            return {
                pos: d.model_copy(deep=True)
                for (rid, pos), d in sorted(self._rows.items())
                if rid == review_id
            }


class MemoryFeedbackRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[str, Feedback] = {}

    def put(self, feedback: Feedback) -> Feedback:
        with self._lock:
            stored = feedback.model_copy(deep=True)
            stored.created_at = stored.created_at or _now()
            existing = self._rows.get(feedback.feedback_id)
            if existing:  # the id is the identity: a conflict never moves the row
                stored.review_id, stored.position = existing.review_id, existing.position
            self._rows[feedback.feedback_id] = stored
            return stored.model_copy(deep=True)

    def get(self, feedback_id: str) -> Feedback | None:
        with self._lock:
            row = self._rows.get(feedback_id)
            return row.model_copy(deep=True) if row else None

    def for_review(self, review_id: str) -> dict[int, Feedback]:
        with self._lock:
            latest: dict[int, Feedback] = {}
            for fb in self._rows.values():
                if fb.review_id != review_id:
                    continue
                best = latest.get(fb.position)
                if best is None or fb.created_at >= best.created_at:
                    latest[fb.position] = fb
            return {pos: fb.model_copy(deep=True) for pos, fb in sorted(latest.items())}


@dataclass
class MemoryStore:
    """Satisfies `app.store.interfaces.Store`."""

    files: MemoryFileStore = field(default_factory=MemoryFileStore)
    documents: MemoryDocumentRepo = field(default_factory=MemoryDocumentRepo)
    verdicts: MemoryVerdictRepo = field(default_factory=MemoryVerdictRepo)
    caches: MemoryCacheRepo = field(default_factory=MemoryCacheRepo)
    reviews: MemoryReviewRepo = field(default_factory=MemoryReviewRepo)
    events: MemoryEventRepo = field(default_factory=MemoryEventRepo)
    decisions: MemoryDecisionRepo = field(default_factory=MemoryDecisionRepo)
    feedback: MemoryFeedbackRepo = field(default_factory=MemoryFeedbackRepo)


def memory_store() -> MemoryStore:
    return MemoryStore()

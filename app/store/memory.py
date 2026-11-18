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
    IDLE_REVIEW_STATUSES,
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
    StoredEvent,
    agreement_rate,
    decided_verdict,
    kept_model_verdict,
)
from app.store.deletion import MemoryDeletionRepo


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryFileStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._created: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def put(self, key: str, data: bytes) -> str:
        with self._lock:
            if key not in self._blobs:
                self._blobs[key] = bytes(data)
                self._created[key] = _now()
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

    def delete(self, key: str) -> bool:
        with self._lock:
            existed = self._blobs.pop(key, None) is not None
            self._created.pop(key, None)
            return existed

    def presign(self, key: str, expires_in: int) -> str | None:
        return None  # like local disk: the API proxies the bytes

    def keys(self) -> list[str]:
        """Every stored key; lets deletion tests assert which blobs are left."""
        with self._lock:
            return sorted(self._blobs)

    def list_keys(self, prefix: str = "") -> list[tuple[str, datetime]]:
        with self._lock:
            return sorted(
                (key, self._created.get(key) or _now())
                for key in self._blobs
                if key.startswith(prefix)
            )


class MemoryDocumentRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.uploads: list[dict] = []
        self._documents: dict[str, Document] = {}
        self._clauses: dict[str, list[ClauseSpan]] = {}
        self._texts: dict[str, str] = {}  # content_hash -> text, stored once
        self._text_created: dict[str, datetime] = {}

    def record_upload(
        self,
        document_id: str,
        filename: str,
        source_type: str,
        size: int,
        document_type: str = "agb",
    ) -> str:
        upload_id = uuid.uuid4().hex
        with self._lock:
            self.uploads.append(
                {
                    "upload_id": upload_id,
                    "document_id": document_id,
                    "filename": filename,
                    "source_type": source_type,
                    "document_type": document_type,
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
                self._text_created.setdefault(clause.content_hash, _now())
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

    def list(self, limit: int = 100, *, owner_id: str | None = None) -> list[Document]:
        with self._lock:
            docs = sorted(
                (d for d in self._documents.values() if owner_id in (None, d.owner_id)),
                key=lambda d: d.created_at,
                reverse=True,
            )
            return [d.model_copy(deep=True) for d in docs[:limit]]

    def text_count(self) -> int:
        """Rows in `clause_texts`; lets tests assert a repeated clause is stored once."""
        with self._lock:
            return len(self._texts)


class MemoryVerdictRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], ClauseVerdict] = {}
        self._created: dict[tuple[str, str], datetime] = {}

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
            key = (verdict.content_hash, verdict.agent_version)
            row = self._rows.setdefault(key, verdict.model_copy(deep=True))
            self._created.setdefault(key, _now())
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
        self._topics: dict[tuple[str, str], str] = {}
        self._obligations: dict[tuple[str, str, str], list[ObligationCode]] = {}
        self._embedding_created: dict[tuple[str, str], datetime] = {}
        self._retrieval_created: dict[tuple[str, str, str], datetime] = {}
        self._topic_created: dict[tuple[str, str], datetime] = {}
        self._obligation_created: dict[tuple[str, str, str], datetime] = {}
        self._summaries: dict[tuple[str, str], str] = {}
        # Phase 9: the review each paragraph was written for (None: before Phase 9).
        self._summary_reviews: dict[tuple[str, str], str | None] = {}
        self._summary_created: dict[tuple[str, str], datetime] = {}

    def get_embedding(self, content_hash: str, model: str) -> list[float] | None:
        with self._lock:
            vec = self._embeddings.get((content_hash, model))
            return list(vec) if vec is not None else None

    def put_embedding(self, content_hash: str, model: str, vector: list[float]) -> None:
        with self._lock:
            key = (content_hash, model)
            self._embeddings.setdefault(key, list(vector))
            self._embedding_created.setdefault(key, _now())

    def get_retrieval(self, content_hash: str, index_hash: str, policy_hash: str) -> list | None:
        with self._lock:
            found = self._retrievals.get((content_hash, index_hash, policy_hash))
            return deepcopy(found) if found is not None else None

    def put_retrieval(
        self, content_hash: str, index_hash: str, policy_hash: str, candidates: list
    ) -> None:
        with self._lock:
            key = (content_hash, index_hash, policy_hash)
            self._retrievals.setdefault(key, deepcopy(candidates))
            self._retrieval_created.setdefault(key, _now())

    def get_topics(self, content_hashes: list[str], model: str) -> dict[str, str]:
        with self._lock:
            return {
                content_hash: topic
                for content_hash in dict.fromkeys(content_hashes)
                if (topic := self._topics.get((content_hash, model))) is not None
            }

    def put_topics(self, model: str, topics: dict[str, str]) -> None:
        with self._lock:
            for content_hash, topic in topics.items():
                key = (content_hash, model)
                self._topics.setdefault(key, topic)
                self._topic_created.setdefault(key, _now())

    def get_obligations(
        self, content_hashes: list[str], model: str, profile: str
    ) -> dict[str, list[ObligationCode]]:
        with self._lock:
            return {
                content_hash: list(found)
                for content_hash in dict.fromkeys(content_hashes)
                if (found := self._obligations.get((content_hash, model, profile))) is not None
            }

    def put_obligations(
        self,
        model: str,
        profile: str,
        obligations: dict[str, list[ObligationCode]],
    ) -> None:
        with self._lock:
            for content_hash, found in obligations.items():
                key = (content_hash, model, profile)
                self._obligations.setdefault(key, list(found))
                self._obligation_created.setdefault(key, _now())

    def get_summary_text(self, decision_set_hash: str, model: str) -> str | None:
        with self._lock:
            return self._summaries.get((decision_set_hash, model))

    def put_summary_text(
        self, decision_set_hash: str, model: str, text: str, *, review_id: str | None = None
    ) -> None:
        with self._lock:
            key = (decision_set_hash, model)
            if key not in self._summaries:
                self._summaries[key] = text
                self._summary_reviews[key] = review_id
                self._summary_created[key] = _now()


class MemoryReviewRepo:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reviews: dict[str, Review] = {}
        self._order: dict[str, int] = {}  # insertion order breaks created_at ties
        self._clauses: dict[str, dict[int, tuple[ClauseStatus, str | None]]] = {}
        self._topics: dict[str, dict[int, str]] = {}
        self._obligations: dict[str, dict[int, list[ObligationCode]]] = {}

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
            # finished_at marks the end of the last run, so the move between idle statuses
            # that a decision makes (awaiting_decisions → finished) does not restamp it.
            was_idle = review.status in IDLE_REVIEW_STATUSES
            review.status = status
            review.error = error
            if status == "running" and review.started_at is None:
                review.started_at = _now()
            if status in IDLE_REVIEW_STATUSES and not was_idle:
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

    def set_clause_topics(self, review_id: str, topics: dict[int, str]) -> None:
        if not topics:
            return
        with self._lock:
            if review_id not in self._reviews:
                raise KeyError(review_id)  # Postgres: foreign key violation
            rows = self._clauses.setdefault(review_id, {})
            for position in topics:
                rows.setdefault(position, ("queued", None))
            self._topics.setdefault(review_id, {}).update(topics)

    def clause_topics(self, review_id: str) -> dict[int, str]:
        with self._lock:
            return dict(sorted(self._topics.get(review_id, {}).items()))

    def set_clause_obligations(
        self, review_id: str, obligations: dict[int, list[ObligationCode]]
    ) -> None:
        if not obligations:
            return
        with self._lock:
            if review_id not in self._reviews:
                raise KeyError(review_id)
            rows = self._clauses.setdefault(review_id, {})
            for position in obligations:
                rows.setdefault(position, ("queued", None))
            stored = self._obligations.setdefault(review_id, {})
            for position, found in obligations.items():
                stored[position] = list(found)

    def clause_obligations(self, review_id: str) -> dict[int, list[ObligationCode]]:
        with self._lock:
            return {
                position: list(found)
                for position, found in sorted(self._obligations.get(review_id, {}).items())
            }


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
        self._events: list[DecisionEvent] = []  # append-only, in event_id order
        self._agent_versions: dict[str, str] = {}  # review_id -> agent_version, for metrics

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
        with self._lock:
            key = (decision.review_id, decision.position)
            previous = self._rows.get(key)
            stored = decision.model_copy(deep=True)
            stored.decided_at = stored.decided_at or _now()
            if previous is None:
                from_verdict, from_basis = decision.model_verdict, list(decision.model_basis)
            else:
                from_verdict, from_basis = decided_verdict(previous)
            to_verdict, to_basis = decided_verdict(stored)
            self._events.append(
                DecisionEvent(
                    event_id=len(self._events) + 1,
                    review_id=stored.review_id,
                    position=stored.position,
                    action=stored.action,
                    previous_action=previous.action if previous else None,
                    from_verdict=from_verdict,
                    from_basis=from_basis,
                    to_verdict=to_verdict,
                    to_basis=to_basis,
                    note=stored.note,
                    decided_by=stored.decided_by,
                    authenticated=authenticated,
                    agent_version=agent_version,
                    request_id=request_id,
                    at=stored.decided_at,
                )
            )
            self._rows[key] = stored
            self._agent_versions[stored.review_id] = agent_version
            return stored.model_copy(deep=True)

    def history(self, review_id: str, position: int | None = None) -> list[DecisionEvent]:
        with self._lock:
            return [
                event.model_copy(deep=True)
                for event in self._events
                if event.review_id == review_id and position in (None, event.position)
            ]

    def current(self) -> list[tuple[str, Decision]]:
        """Every current decision with its review's agent version (for the metrics fake)."""
        with self._lock:
            return [
                (self._agent_versions[rid], d.model_copy(deep=True))
                for (rid, _), d in self._rows.items()
            ]

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

    def delete(self, feedback_id: str) -> None:
        """Remove feedback whose source decision was superseded by a dismissal."""
        with self._lock:
            self._rows.pop(feedback_id, None)

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


class MemoryMetricsRepo:
    """The `decision_agreement` view over the decision fake.

    The view joins decisions to `reviews` for the agent version; the fake takes it from the
    version each decision was stored under, which is the same thing for every caller.
    """

    def __init__(self, decisions: MemoryDecisionRepo) -> None:
        self._decisions = decisions

    def agreement(self, agent_version: str | None = None) -> list[Agreement]:
        tallies: dict[str, Agreement] = {}
        for version, decision in self._decisions.current():
            if decision.model_verdict is None or agent_version not in (None, version):
                continue
            total = tallies.setdefault(version, Agreement(agent_version=version))
            per_class = getattr(total.by_verdict, decision.model_verdict)
            for counts in (total, per_class):
                counts.decided += 1
                kept = kept_model_verdict(decision)
                if kept is None:
                    counts.dismissed += 1
                elif kept:
                    counts.kept += 1
                else:
                    counts.overridden += 1
            if total.last_decided_at is None or decision.decided_at > total.last_decided_at:
                total.last_decided_at = decision.decided_at
        for total in tallies.values():
            counts_list: list[AgreementCounts] = [
                total,
                *(getattr(total.by_verdict, name) for name in AgreementByVerdict.model_fields),
            ]
            for counts in counts_list:
                counts.rate = agreement_rate(counts.kept, counts.overridden)
        return sorted(tallies.values(), key=lambda a: a.last_decided_at, reverse=True)


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
    metrics: MemoryMetricsRepo = field(init=False)
    deletion: MemoryDeletionRepo = field(init=False)

    def __post_init__(self) -> None:
        self.metrics = MemoryMetricsRepo(self.decisions)
        self.deletion = MemoryDeletionRepo(self)


def memory_store() -> MemoryStore:
    return MemoryStore()

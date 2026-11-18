"""Reference-counted deletion and retention queries (Phase 9, 9.B and 9.C).

Both implementations of `app.store.interfaces.DeletionRepo` live here, apart from the other
repositories, because deletion is the one operation that crosses every table: the memory
fake needs the internals of every memory repository, and the Postgres version is one
transaction over all of them. docs/Phase 9 API.md, "Deletion", is the contract;
docs/Phase 9 Design.md explains reference counting, the outbox and the tombstones.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import (
    ActiveReviewError,
    DataClass,
    DeletionReason,
    DeletionReport,
    ErasureTarget,
    ErasureTask,
    Principal,
    Tombstone,
    blob_key,
    checkpoint_key,
)

if TYPE_CHECKING:
    from app.store.memory import MemoryStore

# Postgres: the transaction-local setting that lets a deletion remove rows of the otherwise
# append-only `decision_events` (migration 006). Set with `set local`, so it ends with the
# transaction and no other statement ever sees it.
ERASURE_SETTING = "agb.erasure"

# The orphan sweep leaves younger rows alone: an ingest writes the blob and clause texts
# before the document that references them (docs/Phase 9 API.md, "The orphan sweep").
ORPHAN_MIN_AGE = timedelta(hours=1)
_EPOCH = datetime.min.replace(tzinfo=UTC)

# Postgres: the advisory lock that orders deletions against ingest (9.B). A deletion, the
# orphan sweep and a blob erasure take it exclusively; `PostgresDocumentRepo.save` takes it
# shared, so saves never wait for each other. Under it, the reference counts a deletion
# reads stay true until it commits: two deletions sharing a clause or a source file run
# one after the other (the second sees the first's deletes, so neither leaks the shared
# row), and an ingest that reuses a clause hash either committed before (the row is
# shared) or starts after (and writes the row again).
CONTENT_LOCK = 7_315_002  # arbitrary, fixed: "agb-check content references"

# A failed erasure is retried after 1, 2, 4 … minutes, at most a day apart, so a task that
# keeps failing (Langfuse down, a misconfigured eraser) never blocks newer ones.
ERASURE_BACKOFF = timedelta(minutes=1)
ERASURE_BACKOFF_MAX = timedelta(hours=24)

# The sweep's tombstone: its subject is no document or review.
SWEEP_SUBJECT = "orphans"
SYSTEM_OWNER = "system"


def erasure_backoff(attempts: int) -> timedelta:
    """How long a task waits after its `attempts`-th failure."""
    if attempts >= 20:  # 2**19 minutes is far past the cap already
        return ERASURE_BACKOFF_MAX
    return min(ERASURE_BACKOFF * 2 ** max(0, attempts - 1), ERASURE_BACKOFF_MAX)


def _stem(key: str) -> str:
    """The source hash a blob key (`{source_hash}.pdf|txt`) names."""
    return key.rsplit(".", 1)[0]


def _now() -> datetime:
    return datetime.now(UTC)


def _counts() -> dict[str, int]:
    from app.domain import DELETION_TABLES

    return dict.fromkeys(DELETION_TABLES, 0)


def _stable_id(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode()
    return hashlib.sha256(payload).hexdigest()


def _correction_id(content_hash: str, review_id: str) -> str:
    return _stable_id(content_hash, review_id)


class MemoryDeletionRepo:
    """The in-memory deletion, over the internals of `store`'s repositories."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._tasks: list[ErasureTask] = []
        self._tombstones: list[Tombstone] = []
        self._next_task_id = 1

    def plan_document(self, document_id: str) -> DeletionReport:
        with self._lock:
            return self._document_report(document_id, dry_run=True)

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        with self._lock:
            report = self._document_report(document_id, dry_run=False)
            document = self.store.documents._documents[document_id]
            review_ids = set(report.review_ids)
            content_hashes = {
                clause.content_hash for clause in self.store.documents._clauses[document_id]
            }
            shared = self._shared_hashes(document_id, content_hashes)
            orphaned = content_hashes - shared

            self._remove_reviews(review_ids)
            self.store.documents.uploads[:] = [
                row for row in self.store.documents.uploads if row["document_id"] != document_id
            ]
            self.store.documents._clauses.pop(document_id, None)
            self.store.documents._documents.pop(document_id, None)
            for _name, rows, created, hash_of in self._content_tables():
                for key in [key for key in rows if hash_of(key) in orphaned]:
                    rows.pop(key, None)
                    created.pop(key, None)
            tombstone = self._add_tombstone(
                kind="document",
                subject_id=document_id,
                owner_id=document.owner_id,
                deleted_by=deleted_by,
                reason=reason,
                removed=report.removed,
            )
            report.tombstone = tombstone
            report.erasures = self._queue(report.erasures, tombstone.tombstone_id)
            return report

    def delete_review(
        self, review_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        with self._lock:
            review = self.store.reviews._reviews.get(review_id)
            if review is None:
                raise KeyError(review_id)
            if review.status in ("queued", "running"):
                raise ActiveReviewError([review_id])
            report = self._review_report(review_id, dry_run=False)
            self._remove_reviews({review_id})
            tombstone = self._add_tombstone(
                kind="review",
                subject_id=review_id,
                owner_id=review.owner_id,
                deleted_by=deleted_by,
                reason=reason,
                removed=report.removed,
            )
            report.tombstone = tombstone
            report.erasures = self._queue(report.erasures, tombstone.tombstone_id)
            return report

    def sweep_orphans(
        self,
        *,
        blobs: Iterable[tuple[str, datetime]] = (),
        summaries_before: datetime | None = None,
    ) -> dict[str, int]:
        with self._lock:
            # Like Postgres: rows younger than ORPHAN_MIN_AGE may belong to an upload that
            # has written its clause texts (or blob) but not yet its document.
            cutoff = _now() - ORPHAN_MIN_AGE
            referenced = {
                clause.content_hash
                for clauses in self.store.documents._clauses.values()
                for clause in clauses
            }
            removed = _counts()
            traces: list[tuple[ErasureTarget, str]] = []
            for name, rows, created, hash_of in self._content_tables():
                doomed = [
                    key
                    for key in rows
                    if hash_of(key) not in referenced and created.get(key, _EPOCH) < cutoff
                ]
                removed[name] = len(doomed)
                for key in doomed:
                    row = rows.pop(key, None)
                    created.pop(key, None)
                    if name == "verdicts" and row is not None and row.trace_id:
                        traces.append(("trace", row.trace_id))

            self._sweep_review_orphans(cutoff, summaries_before, removed)

            live_blobs = {
                blob_key(document) for document in self.store.documents._documents.values()
            }
            orphan_blobs = sorted(
                {key for key, modified in blobs if key not in live_blobs and modified < cutoff}
            )
            removed["blobs"] = len(orphan_blobs)
            tombstone_id = None
            if any(removed.values()):
                tombstone_id = self._add_tombstone(
                    kind="sweep",
                    subject_id=SWEEP_SUBJECT,
                    owner_id=SYSTEM_OWNER,
                    deleted_by=None,
                    reason="orphan_sweep",
                    removed=removed,
                ).tombstone_id
            planned = [
                self._planned(target, key)
                for target, key in traces + [("blob", key) for key in orphan_blobs]
            ]
            self._queue(planned, tombstone_id)
            return removed

    def _sweep_review_orphans(
        self, cutoff: datetime, summaries_before: datetime | None, removed: dict[str, int]
    ) -> None:
        """Rows of a review that no longer exists: written while its deletion ran."""
        live = set(self.store.reviews._reviews)
        reviews, events, decisions, feedback = (
            self.store.reviews,
            self.store.events,
            self.store.decisions,
            self.store.feedback,
        )
        for review_id in [key for key in reviews._clauses if key not in live]:
            removed["review_clauses"] += len(reviews._clauses.pop(review_id))
        for review_id in [key for key in reviews._topics if key not in live]:
            reviews._topics.pop(review_id)
        for review_id in [
            key
            for key, rows in events._events.items()
            if key not in live and all((row.created_at or _now()) < cutoff for row in rows)
        ]:
            removed["run_events"] += len(events._events.pop(review_id))
        doomed = [
            key
            for key, row in decisions._rows.items()
            if key[0] not in live and (row.decided_at or _now()) < cutoff
        ]
        for key in doomed:
            decisions._rows.pop(key)
        removed["decisions"] += len(doomed)
        kept = [row for row in decisions._events if row.review_id in live or row.at >= cutoff]
        removed["decision_events"] += len(decisions._events) - len(kept)
        decisions._events = kept
        doomed_feedback = [
            key
            for key, row in feedback._rows.items()
            if row.review_id not in live and (row.created_at or _now()) < cutoff
        ]
        for key in doomed_feedback:
            feedback._rows.pop(key)
        removed["feedback"] += len(doomed_feedback)

        caches = self.store.caches
        doomed_summaries = []
        for key, review_id in caches._summary_reviews.items():
            created = caches._summary_created.get(key, _now())
            if review_id is None:
                if summaries_before is not None and created < summaries_before:
                    doomed_summaries.append(key)
            elif review_id not in live and created < cutoff:
                doomed_summaries.append(key)
        for key in doomed_summaries:
            self._drop_summary(key)
        removed["summary_texts"] += len(doomed_summaries)

    def _drop_summary(self, key: tuple[str, str]) -> None:
        caches = self.store.caches
        caches._summaries.pop(key, None)
        caches._summary_reviews.pop(key, None)
        caches._summary_created.pop(key, None)

    def erase_blob(self, key: str, delete: Callable[[str], object]) -> bool:
        with self._lock, self.store.documents._lock:
            if any(
                blob_key(document) == key for document in self.store.documents._documents.values()
            ):
                return False
            delete(key)
            return True

    def expired(self, data_class: DataClass, before: datetime, limit: int = 100) -> list[str]:
        with self._lock:
            if data_class == "files":
                uploaded = {
                    document_id: max(
                        row["uploaded_at"]
                        for row in self.store.documents.uploads
                        if row["document_id"] == document_id
                    )
                    for document_id in self.store.documents._documents
                    if any(
                        row["document_id"] == document_id for row in self.store.documents.uploads
                    )
                }
                # No timestamp at all (never in practice: save stamps created_at) is not
                # "expired since the epoch": such a document is kept.
                candidates = [
                    (at, document_id)
                    for document_id, document in self.store.documents._documents.items()
                    if (at := uploaded.get(document_id) or document.created_at) is not None
                ]
            elif data_class == "reviews":
                candidates = [
                    (review.finished_at, review_id)
                    for review_id, review in self.store.reviews._reviews.items()
                    if review.finished_at is not None and review.status not in ("queued", "running")
                ]
            else:
                candidates = [
                    (review.created_at or _EPOCH, review.trace_id)
                    for review in self.store.reviews._reviews.values()
                    if review.trace_id
                ]
                candidates += [
                    (self.store.verdicts._created.get(key, _EPOCH), verdict.trace_id)
                    for key, verdict in self.store.verdicts._rows.items()
                    if verdict.trace_id
                ]
                queued = {task.key for task in self._tasks if task.target == "trace"}
                first: dict[str, datetime] = {}
                for at, key in candidates:
                    if key not in queued:
                        first[key] = min(at, first.get(key, at))
                candidates = [(at, key) for key, at in first.items()]
            return [key for at, key in sorted(candidates) if at < before][:limit]

    def pending_erasures(self, limit: int = 100, *, due: bool = False) -> list[ErasureTask]:
        with self._lock:
            now = _now()
            rows = sorted(
                (
                    task
                    for task in self._tasks
                    if task.done_at is None
                    and not (due and (task.next_attempt_at or task.requested_at) > now)
                ),
                key=lambda task: (task.next_attempt_at or task.requested_at, task.task_id),
            )
            return [task.model_copy(deep=True) for task in rows[:limit]]

    def finish_erasure(self, task_id: int, error: str | None = None) -> None:
        with self._lock:
            task = next((row for row in self._tasks if row.task_id == task_id), None)
            if task is None:
                raise KeyError(task_id)
            if task.done_at is not None:
                return
            task.attempts += 1
            task.last_error = error
            if error is None:
                task.done_at = _now()
            else:
                task.next_attempt_at = _now() + erasure_backoff(task.attempts)

    def queue_erasures(self, tasks: list[tuple[ErasureTarget, str]]) -> list[ErasureTask]:
        with self._lock:
            planned = [
                ErasureTask(task_id=0, target=target, key=key, requested_at=_now())
                for target, key in tasks
            ]
            return self._queue(planned, None)

    def tombstones(self, subject_id: str | None = None) -> list[Tombstone]:
        with self._lock:
            return [
                row.model_copy(deep=True)
                for row in self._tombstones
                if subject_id in (None, row.subject_id)
            ]

    def _document_report(self, document_id: str, *, dry_run: bool) -> DeletionReport:
        document = self.store.documents._documents.get(document_id)
        if document is None:
            raise KeyError(document_id)
        active = sorted(
            review.review_id
            for review in self.store.reviews._reviews.values()
            if review.document_id == document_id and review.status in ("queued", "running")
        )
        if active:
            raise ActiveReviewError(active)
        review_ids = sorted(
            review.review_id
            for review in self.store.reviews._reviews.values()
            if review.document_id == document_id
        )
        clauses = self.store.documents._clauses.get(document_id, [])
        hashes = {clause.content_hash for clause in clauses}
        shared = self._shared_hashes(document_id, hashes)
        orphaned = hashes - shared
        removed = self._review_counts(set(review_ids))
        removed.update(
            {
                "documents": 1,
                "uploads": sum(
                    row["document_id"] == document_id for row in self.store.documents.uploads
                ),
                "document_clauses": len(clauses),
                "clause_texts": sum(key in self.store.documents._texts for key in orphaned),
                "verdicts": sum(key[0] in orphaned for key in self.store.verdicts._rows),
                "embeddings": sum(key[0] in orphaned for key in self.store.caches._embeddings),
                "retrievals": sum(key[0] in orphaned for key in self.store.caches._retrievals),
                "topics": sum(key[0] in orphaned for key in self.store.caches._topics),
                "obligation_routes": sum(
                    key[0] in orphaned for key in self.store.caches._obligations
                ),
            }
        )
        kept = {
            "clause_texts": sum(key in self.store.documents._texts for key in shared),
            "verdicts": sum(key[0] in shared for key in self.store.verdicts._rows),
            "embeddings": sum(key[0] in shared for key in self.store.caches._embeddings),
            "retrievals": sum(key[0] in shared for key in self.store.caches._retrievals),
            "topics": sum(key[0] in shared for key in self.store.caches._topics),
            "obligation_routes": sum(key[0] in shared for key in self.store.caches._obligations),
        }
        erasures = self._external_tasks(set(review_ids), orphaned)
        if not any(
            other.document_id != document_id and other.source_hash == document.source_hash
            for other in self.store.documents._documents.values()
        ):
            erasures.append(self._planned("blob", blob_key(document)))
        return DeletionReport(
            document_id=document_id,
            review_ids=review_ids,
            dry_run=dry_run,
            removed=removed,
            kept_shared=kept,
            erasures=erasures,
        )

    def _review_report(self, review_id: str, *, dry_run: bool) -> DeletionReport:
        if review_id not in self.store.reviews._reviews:
            raise KeyError(review_id)
        review_ids = {review_id}
        return DeletionReport(
            review_ids=[review_id],
            dry_run=dry_run,
            removed=self._review_counts(review_ids),
            erasures=self._external_tasks(review_ids, set()),
        )

    def _review_counts(self, review_ids: set[str]) -> dict[str, int]:
        removed = _counts()
        removed["reviews"] = len(review_ids)
        removed["review_clauses"] = sum(
            len(self.store.reviews._clauses.get(review_id, {})) for review_id in review_ids
        )
        removed["run_events"] = sum(
            len(self.store.events._events.get(review_id, [])) for review_id in review_ids
        )
        removed["decisions"] = sum(
            review_id in review_ids for review_id, _ in self.store.decisions._rows
        )
        removed["decision_events"] = sum(
            event.review_id in review_ids for event in self.store.decisions._events
        )
        removed["feedback"] = sum(
            row.review_id in review_ids for row in self.store.feedback._rows.values()
        )
        removed["summary_texts"] = sum(
            review_id in review_ids
            for review_id in self.store.caches._summary_reviews.values()
            if review_id is not None
        )
        return removed

    def _external_tasks(self, review_ids: set[str], orphaned_hashes: set[str]) -> list[ErasureTask]:
        tasks: list[ErasureTask] = []
        for review_id in sorted(review_ids):
            review = self.store.reviews._reviews[review_id]
            if review.trace_id:
                tasks.append(self._planned("trace", review.trace_id))
                tasks.append(self._planned("score", _stable_id("lawyer_agreement", review_id)))
            tasks.append(self._planned("checkpoint", checkpoint_key(review_id)))
            for (rid, position), decision in self.store.decisions._rows.items():
                if rid == review_id and decision.action == "dismiss":
                    feedback_id = _stable_id(review_id, position, "decision")
                    tasks.append(self._planned("score", _stable_id(feedback_id, "dismissed")))
            correction_positions = {
                position
                for (rid, position), decision in self.store.decisions._rows.items()
                if rid == review_id and decision.action == "override"
            }
            correction_positions.update(
                event.position
                for event in self.store.decisions._events
                if event.review_id == review_id and event.action == "override"
            )
            for position in correction_positions:
                clause = next(
                    (
                        item
                        for item in self.store.documents._clauses.get(review.document_id, [])
                        if item.position == position
                    ),
                    None,
                )
                if clause:
                    tasks.append(
                        self._planned(
                            "dataset_item", _correction_id(clause.content_hash, review_id)
                        )
                    )
        for feedback in self.store.feedback._rows.values():
            if feedback.review_id not in review_ids:
                continue
            tasks.extend(
                self._planned("score", _stable_id(feedback.feedback_id, step))
                for step, observation_id in feedback.observations.items()
                if observation_id is not None
            )
        tasks.extend(
            self._planned("trace", verdict.trace_id)
            for (content_hash, _), verdict in self.store.verdicts._rows.items()
            if content_hash in orphaned_hashes and verdict.trace_id
        )
        return self._dedupe_tasks(tasks)

    def _content_tables(self) -> list[tuple[str, dict, dict, Any]]:
        """Each content-addressed table: its rows, their creation times, and the key's hash."""
        documents, verdicts, caches = self.store.documents, self.store.verdicts, self.store.caches
        whole, first = (lambda key: key), (lambda key: key[0])
        return [
            ("clause_texts", documents._texts, documents._text_created, whole),
            ("verdicts", verdicts._rows, verdicts._created, first),
            ("embeddings", caches._embeddings, caches._embedding_created, first),
            ("retrievals", caches._retrievals, caches._retrieval_created, first),
            ("topics", caches._topics, caches._topic_created, first),
            (
                "obligation_routes",
                caches._obligations,
                caches._obligation_created,
                first,
            ),
        ]

    def _shared_hashes(self, document_id: str, hashes: set[str]) -> set[str]:
        return {
            clause.content_hash
            for other_id, clauses in self.store.documents._clauses.items()
            if other_id != document_id
            for clause in clauses
            if clause.content_hash in hashes
        }

    def _remove_reviews(self, review_ids: set[str]) -> None:
        for review_id in review_ids:
            self.store.reviews._reviews.pop(review_id, None)
            self.store.reviews._order.pop(review_id, None)
            self.store.reviews._clauses.pop(review_id, None)
            self.store.reviews._topics.pop(review_id, None)
            self.store.events._events.pop(review_id, None)
        self.store.decisions._rows = {
            key: row for key, row in self.store.decisions._rows.items() if key[0] not in review_ids
        }
        self.store.decisions._events = [
            row for row in self.store.decisions._events if row.review_id not in review_ids
        ]
        for review_id in review_ids:
            self.store.decisions._agent_versions.pop(review_id, None)
        self.store.feedback._rows = {
            key: row
            for key, row in self.store.feedback._rows.items()
            if row.review_id not in review_ids
        }
        for key in [
            key
            for key, review_id in self.store.caches._summary_reviews.items()
            if review_id in review_ids
        ]:
            self._drop_summary(key)

    def _add_tombstone(
        self,
        *,
        kind: str,
        subject_id: str,
        owner_id: str,
        deleted_by: Principal | None,
        reason: DeletionReason,
        removed: dict[str, int],
    ) -> Tombstone:
        tombstone = Tombstone(
            tombstone_id=uuid.uuid4().hex,
            kind=kind,
            subject_id=subject_id,
            owner_id=owner_id,
            deleted_by=deleted_by.owner_id if deleted_by else None,
            authenticated=deleted_by.authenticated if deleted_by else False,
            reason=reason,
            removed=deepcopy(removed),
            at=_now(),
        )
        self._tombstones.append(tombstone)
        return tombstone.model_copy(deep=True)

    def _queue(self, tasks: list[ErasureTask], tombstone_id: str | None) -> list[ErasureTask]:
        queued: list[ErasureTask] = []
        for planned in self._dedupe_tasks(tasks):
            existing = next(
                (
                    row
                    for row in self._tasks
                    if row.done_at is None
                    and (row.target, row.key) == (planned.target, planned.key)
                ),
                None,
            )
            if existing is None:
                existing = planned.model_copy(
                    update={
                        "task_id": self._next_task_id,
                        "tombstone_id": tombstone_id,
                        "next_attempt_at": planned.requested_at,
                    }
                )
                self._next_task_id += 1
                self._tasks.append(existing)
            queued.append(existing.model_copy(deep=True))
        return queued

    @staticmethod
    def _planned(target: ErasureTarget, key: str) -> ErasureTask:
        return ErasureTask(task_id=0, target=target, key=key, requested_at=_now())

    @staticmethod
    def _dedupe_tasks(tasks: list[ErasureTask]) -> list[ErasureTask]:
        unique: dict[tuple[str, str], ErasureTask] = {}
        for task in tasks:
            unique.setdefault((task.target, task.key), task)
        return list(unique.values())


class PostgresDeletionRepo:
    """The Postgres deletion: one transaction per document or review."""

    def __init__(self, connect: Any) -> None:
        self._connect = connect

    def plan_document(self, document_id: str) -> DeletionReport:
        with self._connect() as conn:
            report, _orphaned, _document = self._document_report(
                conn, document_id, dry_run=True, lock=False
            )
            return report

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        with self._connect() as conn:
            # Lock order: the content lock, then the document row, then its review rows
            # (inside `_document_report`); ingest takes the content lock first as well.
            conn.execute("select pg_advisory_xact_lock(%s)", (CONTENT_LOCK,))
            report, orphaned, document = self._document_report(
                conn, document_id, dry_run=False, lock=True
            )
            conn.execute(f"set local {ERASURE_SETTING} = 'on'")
            self._delete_review_rows(conn, report.review_ids, report.removed)
            report.removed["uploads"] = _delete_count(
                conn, "delete from uploads where document_id = %s", (document_id,)
            )
            report.removed["document_clauses"] = _delete_count(
                conn, "delete from document_clauses where document_id = %s", (document_id,)
            )
            report.removed["documents"] = _delete_count(
                conn, "delete from documents where document_id = %s", (document_id,)
            )
            self._delete_content_rows(conn, orphaned, report.removed)
            tombstone = self._insert_tombstone(
                conn,
                kind="document",
                subject_id=document_id,
                owner_id=document["owner_id"],
                deleted_by=deleted_by,
                reason=reason,
                removed=report.removed,
            )
            report.tombstone = tombstone
            report.erasures = self._queue_conn(
                conn,
                [(task.target, task.key) for task in report.erasures],
                tombstone.tombstone_id,
            )
            return report

    def delete_review(
        self, review_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        with self._connect() as conn:
            review = _pg_row(
                conn,
                "select review_id, owner_id, status from reviews where review_id = %s for update",
                (review_id,),
            )
            if review is None:
                raise KeyError(review_id)
            if review["status"] in ("queued", "running"):
                raise ActiveReviewError([review_id])
            report = self._review_report(conn, review_id)
            conn.execute(f"set local {ERASURE_SETTING} = 'on'")
            self._delete_review_rows(conn, [review_id], report.removed)
            tombstone = self._insert_tombstone(
                conn,
                kind="review",
                subject_id=review_id,
                owner_id=review["owner_id"],
                deleted_by=deleted_by,
                reason=reason,
                removed=report.removed,
            )
            report.tombstone = tombstone
            report.erasures = self._queue_conn(
                conn,
                [(task.target, task.key) for task in report.erasures],
                tombstone.tombstone_id,
            )
            return report

    def sweep_orphans(
        self,
        *,
        blobs: Iterable[tuple[str, datetime]] = (),
        summaries_before: datetime | None = None,
    ) -> dict[str, int]:
        removed = _counts()
        listed = list(blobs)
        with self._connect() as conn:
            # Exclusive, like a deletion: no ingest is between its clause texts and its
            # clauses while the sweep decides what nothing references.
            conn.execute("select pg_advisory_xact_lock(%s)", (CONTENT_LOCK,))
            conn.execute(f"set local {ERASURE_SETTING} = 'on'")
            unreferenced = (
                " candidate.created_at < clock_timestamp() - interval '1 hour'"
                " and not exists (select 1 from document_clauses d"
                " where d.content_hash = candidate.content_hash)"
                + self._eval_guard(conn, "candidate.content_hash")
            )
            verdict_traces = _pg_column(
                conn,
                "select distinct trace_id from verdicts candidate where trace_id is not null"
                " and" + unreferenced,
            )
            for table in (
                "verdicts",
                "embeddings",
                "retrievals",
                "topics",
                "obligation_routes",
                "clause_texts",
            ):
                removed[table] = _delete_count(
                    conn, f"delete from {table} candidate where" + unreferenced
                )
            self._sweep_review_orphans(conn, summaries_before, removed)

            cutoff = _now() - ORPHAN_MIN_AGE
            old = sorted({key for key, modified in listed if modified < cutoff})
            used = set(
                _pg_column(
                    conn,
                    "select distinct source_hash from documents where source_hash = any(%s)",
                    ([_stem(key) for key in old],),
                )
                if old
                else []
            )
            orphan_blobs = [key for key in old if _stem(key) not in used]
            removed["blobs"] = len(orphan_blobs)
            tombstone_id = None
            if any(removed.values()):
                tombstone_id = self._insert_tombstone(
                    conn,
                    kind="sweep",
                    subject_id=SWEEP_SUBJECT,
                    owner_id=SYSTEM_OWNER,
                    deleted_by=None,
                    reason="orphan_sweep",
                    removed=removed,
                ).tombstone_id
            self._queue_conn(
                conn,
                [("trace", key) for key in verdict_traces]
                + [("blob", key) for key in orphan_blobs],
                tombstone_id,
            )
        return removed

    @staticmethod
    def _sweep_review_orphans(
        conn: Any, summaries_before: datetime | None, removed: dict[str, int]
    ) -> None:
        """Rows of a review that no longer exists: a decision, feedback or event written
        while its review was being deleted (these tables have no foreign key to reviews)."""
        gone = " and not exists (select 1 from reviews r where r.review_id = candidate.review_id)"
        old = " < clock_timestamp() - interval '1 hour'"
        for table, stamp in (
            ("decision_events", "at"),
            ("decisions", "decided_at"),
            ("feedback", "created_at"),
            ("run_events", "created_at"),
        ):
            removed[table] += _delete_count(
                conn, f"delete from {table} candidate where candidate.{stamp}" + old + gone
            )
        removed["summary_texts"] += _delete_count(
            conn,
            "delete from summary_texts candidate where candidate.review_id is not null"
            " and candidate.created_at" + old + gone,
        )
        if summaries_before is not None:
            removed["summary_texts"] += _delete_count(
                conn,
                "delete from summary_texts where review_id is null and created_at < %s",
                (summaries_before,),
            )

    def erase_blob(self, key: str, delete: Callable[[str], object]) -> bool:
        with self._connect() as conn:
            # Held across the delete: an ingest cannot commit a document naming the key
            # until the blob is gone, and then re-puts it (`app.ingest`).
            conn.execute("select pg_advisory_xact_lock(%s)", (CONTENT_LOCK,))
            if _pg_row(
                conn,
                "select 1 as found from documents where source_hash = %s limit 1",
                (_stem(key),),
            ):
                return False
            delete(key)
            return True

    def expired(self, data_class: DataClass, before: datetime, limit: int = 100) -> list[str]:
        if limit < 1:
            return []
        with self._connect() as conn:
            if data_class == "files":
                sql = (
                    "select d.document_id as id from documents d"
                    " left join uploads u on u.document_id = d.document_id"
                    " group by d.document_id, d.created_at"
                    " having coalesce(max(u.uploaded_at), d.created_at) < %s"
                    " order by coalesce(max(u.uploaded_at), d.created_at), d.document_id limit %s"
                )
            elif data_class == "reviews":
                sql = (
                    "select review_id as id from reviews where finished_at < %s"
                    " and status not in ('queued', 'running')"
                    " order by finished_at, review_id limit %s"
                )
            else:
                sql = (
                    "select source.id from ("
                    " select trace_id as id, created_at from reviews where trace_id is not null"
                    " union all"
                    " select trace_id as id, created_at from verdicts where trace_id is not null"
                    ") source where source.created_at < %s"
                    " and not exists (select 1 from erasure_tasks task"
                    " where task.target = 'trace' and task.key = source.id)"
                    " group by source.id order by min(source.created_at), source.id limit %s"
                )
            return _pg_column(conn, sql, (before, limit))

    def pending_erasures(self, limit: int = 100, *, due: bool = False) -> list[ErasureTask]:
        with self._connect() as conn:
            rows = _pg_rows(
                conn,
                f"select {_TASK_COLUMNS} from erasure_tasks where done_at is null"
                " and (not %s or next_attempt_at <= clock_timestamp())"
                " order by next_attempt_at, task_id limit %s",
                (due, limit),
            )
        return [ErasureTask(**row) for row in rows]

    def finish_erasure(self, task_id: int, error: str | None = None) -> None:
        with self._connect() as conn:
            if error is None:
                cursor = conn.execute(
                    "update erasure_tasks set attempts = attempts + 1, last_error = null,"
                    " done_at = clock_timestamp() where task_id = %s and done_at is null",
                    (task_id,),
                )
            else:
                # `attempts` on the right is the value before this failure: 1, 2, 4 … minutes.
                cursor = conn.execute(
                    "update erasure_tasks set attempts = attempts + 1, last_error = %s,"
                    " next_attempt_at = clock_timestamp() + least("
                    "interval '1 minute' * power(2, least(attempts, 20)), interval '24 hours')"
                    " where task_id = %s and done_at is null",
                    (error, task_id),
                )
            if cursor.rowcount == 0:
                found = _pg_row(
                    conn, "select task_id from erasure_tasks where task_id = %s", (task_id,)
                )
                if found is None:
                    raise KeyError(task_id)

    def queue_erasures(self, tasks: list[tuple[ErasureTarget, str]]) -> list[ErasureTask]:
        with self._connect() as conn:
            return self._queue_conn(conn, tasks, None)

    def tombstones(self, subject_id: str | None = None) -> list[Tombstone]:
        with self._connect() as conn:
            rows = _pg_rows(
                conn,
                "select tombstone_id, kind, subject_id, owner_id, deleted_by, authenticated,"
                " reason, removed, at from deletion_tombstones"
                " where (%s::text is null or subject_id = %s) order by at, tombstone_id",
                (subject_id, subject_id),
            )
        return [Tombstone(**row) for row in rows]

    def _document_report(
        self, conn: Any, document_id: str, *, dry_run: bool, lock: bool
    ) -> tuple[DeletionReport, list[str], dict]:
        """The report, the orphaned hashes (computed once: the delete reuses them), and
        the document row. With `lock`, the document and its review rows are locked."""
        suffix = " for update" if lock else ""
        document = _pg_row(
            conn,
            "select document_id, owner_id, source_hash, source_type from documents"
            " where document_id = %s" + suffix,
            (document_id,),
        )
        if document is None:
            raise KeyError(document_id)
        # Locked: a decision or status change of one of them waits for this transaction.
        reviews = _pg_rows(
            conn,
            "select review_id, status from reviews where document_id = %s order by review_id"
            + suffix,
            (document_id,),
        )
        active = [row["review_id"] for row in reviews if row["status"] in ("queued", "running")]
        if active:
            raise ActiveReviewError(active)
        review_ids = [row["review_id"] for row in reviews]
        hashes = _pg_column(
            conn,
            "select distinct content_hash from document_clauses where document_id = %s",
            (document_id,),
        )
        orphaned, shared = self._partition_hashes(conn, document_id, hashes)
        removed = self._review_counts(conn, review_ids)
        removed.update(
            {
                "documents": 1,
                "uploads": _pg_count(conn, "uploads", "document_id = %s", (document_id,)),
                "document_clauses": _pg_count(
                    conn, "document_clauses", "document_id = %s", (document_id,)
                ),
            }
        )
        kept = _counts_for_hashes(conn, shared)
        removed.update(_counts_for_hashes(conn, orphaned))
        tasks = self._external_tasks(conn, review_ids, orphaned)
        other_blob_users = _pg_count(
            conn,
            "documents",
            "source_hash = %s and document_id <> %s",
            (document["source_hash"], document_id),
        )
        if not other_blob_users:
            suffix = "txt" if document["source_type"] == "text" else "pdf"
            tasks.append(("blob", f"{document['source_hash']}.{suffix}"))
        report = DeletionReport(
            document_id=document_id,
            review_ids=review_ids,
            dry_run=dry_run,
            removed=removed,
            kept_shared={key: kept[key] for key in _CONTENT_TABLES},
            erasures=[
                ErasureTask(task_id=0, target=target, key=key, requested_at=_now())
                for target, key in _dedupe_pairs(tasks)
            ],
        )
        return report, orphaned, document

    def _review_report(self, conn: Any, review_id: str) -> DeletionReport:
        tasks = self._external_tasks(conn, [review_id], [])
        return DeletionReport(
            review_ids=[review_id],
            removed=self._review_counts(conn, [review_id]),
            erasures=[
                ErasureTask(task_id=0, target=target, key=key, requested_at=_now())
                for target, key in _dedupe_pairs(tasks)
            ],
        )

    def _review_counts(self, conn: Any, review_ids: list[str]) -> dict[str, int]:
        removed = _counts()
        if not review_ids:
            return removed
        for table in _REVIEW_TABLES:
            removed[table] = _pg_count(conn, table, "review_id = any(%s)", (review_ids,))
        removed["reviews"] = len(review_ids)
        removed["summary_texts"] = _pg_count(
            conn, "summary_texts", "review_id = any(%s)", (review_ids,)
        )
        return removed

    def _external_tasks(
        self, conn: Any, review_ids: list[str], orphaned_hashes: list[str]
    ) -> list[tuple[ErasureTarget, str]]:
        tasks: list[tuple[ErasureTarget, str]] = []
        if review_ids:
            tasks.extend(
                ("trace", key)
                for key in _pg_column(
                    conn,
                    "select trace_id from reviews where review_id = any(%s)"
                    " and trace_id is not null",
                    (review_ids,),
                )
            )
            tasks.extend(
                ("score", _stable_id("lawyer_agreement", review_id))
                for review_id in review_ids
                if _pg_count(
                    conn,
                    "reviews",
                    "review_id = %s and trace_id is not null",
                    (review_id,),
                )
            )
            tasks.extend(("checkpoint", checkpoint_key(review_id)) for review_id in review_ids)
            feedback_rows = _pg_rows(
                conn,
                "select feedback_id, observations from feedback where review_id = any(%s)",
                (review_ids,),
            )
            for feedback in feedback_rows:
                tasks.extend(
                    ("score", _stable_id(feedback["feedback_id"], step))
                    for step, observation_id in (feedback["observations"] or {}).items()
                    if observation_id is not None
                )
            dismissed = _pg_rows(
                conn,
                "select review_id, position from decisions"
                " where review_id = any(%s) and action = 'dismiss'",
                (review_ids,),
            )
            for decision in dismissed:
                feedback_id = _stable_id(decision["review_id"], decision["position"], "decision")
                tasks.append(("score", _stable_id(feedback_id, "dismissed")))
            corrections = _pg_rows(
                conn,
                "select distinct d.review_id, c.content_hash from decision_events d"
                " join reviews r on r.review_id = d.review_id"
                " join document_clauses c on c.document_id = r.document_id"
                " and c.position = d.position"
                " where d.review_id = any(%s) and d.action = 'override'",
                (review_ids,),
            )
            tasks.extend(
                (
                    "dataset_item",
                    _correction_id(row["content_hash"], row["review_id"]),
                )
                for row in corrections
            )
        if orphaned_hashes:
            tasks.extend(
                ("trace", key)
                for key in _pg_column(
                    conn,
                    "select trace_id from verdicts where content_hash = any(%s)"
                    " and trace_id is not null",
                    (orphaned_hashes,),
                )
            )
        return _dedupe_pairs(tasks)

    def _partition_hashes(
        self, conn: Any, document_id: str, hashes: list[str]
    ) -> tuple[list[str], list[str]]:
        if not hashes:
            return [], []
        shared = set(
            _pg_column(
                conn,
                "select distinct content_hash from document_clauses"
                " where document_id <> %s and content_hash = any(%s)",
                (document_id, hashes),
            )
        )
        if self._has_eval_clauses(conn):
            shared.update(
                _pg_column(
                    conn,
                    "select distinct content_hash from clauses"
                    " where content_hash = any(%s) and content_hash is not null",
                    (hashes,),
                )
            )
        return (
            [key for key in hashes if key not in shared],
            [key for key in hashes if key in shared],
        )

    @staticmethod
    def _delete_review_rows(
        conn: Any, review_ids: list[str], removed: dict[str, int] | None = None
    ) -> None:
        if not review_ids:
            return
        target = removed
        for table in (
            "decision_events",
            "decisions",
            "feedback",
            "run_events",
            "review_clauses",
            "summary_texts",
            "reviews",
        ):
            count = _delete_count(
                conn, f"delete from {table} where review_id = any(%s)", (review_ids,)
            )
            if target is not None:
                target[table] = count

    def _delete_content_rows(self, conn: Any, hashes: list[str], removed: dict[str, int]) -> None:
        if not hashes:
            return
        # Re-checked in the statement itself (the document's own clauses are gone by now):
        # the content lock already keeps ingest out, this keeps a missed lock harmless.
        unreferenced = (
            " and not exists (select 1 from document_clauses d"
            " where d.content_hash = candidate.content_hash)"
            + self._eval_guard(conn, "candidate.content_hash")
        )
        for table in (
            "verdicts",
            "embeddings",
            "retrievals",
            "topics",
            "obligation_routes",
            "clause_texts",
        ):
            removed[table] = _delete_count(
                conn,
                f"delete from {table} candidate where candidate.content_hash = any(%s)"
                + unreferenced,
                (hashes,),
            )

    @staticmethod
    def _insert_tombstone(
        conn: Any,
        *,
        kind: str,
        subject_id: str,
        owner_id: str,
        deleted_by: Principal | None,
        reason: DeletionReason,
        removed: dict[str, int],
    ) -> Tombstone:
        row = _pg_row(
            conn,
            "insert into deletion_tombstones"
            " (tombstone_id, kind, subject_id, owner_id, deleted_by, authenticated,"
            " reason, removed)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s)"
            " returning tombstone_id, kind, subject_id, owner_id, deleted_by, authenticated,"
            " reason, removed, at",
            (
                uuid.uuid4().hex,
                kind,
                subject_id,
                owner_id,
                deleted_by.owner_id if deleted_by else None,
                deleted_by.authenticated if deleted_by else False,
                reason,
                Jsonb(removed),
            ),
        )
        return Tombstone(**row)

    @staticmethod
    def _queue_conn(
        conn: Any,
        tasks: list[tuple[ErasureTarget, str]],
        tombstone_id: str | None,
    ) -> list[ErasureTask]:
        rows: list[ErasureTask] = []
        for target, key in _dedupe_pairs(tasks):
            row = _pg_row(
                conn,
                "insert into erasure_tasks (target, key, tombstone_id) values (%s, %s, %s)"
                " on conflict (target, key) where done_at is null do nothing"
                f" returning {_TASK_COLUMNS}",
                (target, key, tombstone_id),
            )
            if row is None:
                row = _pg_row(
                    conn,
                    f"select {_TASK_COLUMNS} from erasure_tasks"
                    " where target = %s and key = %s and done_at is null",
                    (target, key),
                )
            rows.append(ErasureTask(**row))
        return rows

    @staticmethod
    def _has_eval_clauses(conn: Any) -> bool:
        return bool(
            _pg_row(
                conn,
                "select 1 from information_schema.columns where table_schema = current_schema()"
                " and table_name = 'clauses' and column_name = 'content_hash'",
            )
        )

    def _eval_guard(self, conn: Any, expression: str) -> str:
        if not self._has_eval_clauses(conn):
            return ""
        return f" and not exists (select 1 from clauses e where e.content_hash = {expression})"


_CONTENT_TABLES = (
    "clause_texts",
    "verdicts",
    "embeddings",
    "retrievals",
    "topics",
    "obligation_routes",
)
_TASK_COLUMNS = (
    "task_id, target, key, tombstone_id, requested_at, attempts, last_error, done_at,"
    " next_attempt_at"
)
_REVIEW_TABLES = (
    "review_clauses",
    "run_events",
    "decisions",
    "decision_events",
    "feedback",
)


def _pg_rows(conn: Any, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall() if cursor.description else []


def _pg_row(conn: Any, sql: str, params: tuple = ()) -> dict | None:
    rows = _pg_rows(conn, sql, params)
    return rows[0] if rows else None


def _pg_column(conn: Any, sql: str, params: tuple = ()) -> list[str]:
    return [next(iter(row.values())) for row in _pg_rows(conn, sql, params)]


def _pg_count(conn: Any, table: str, where: str, params: tuple) -> int:
    row = _pg_row(conn, f"select count(*) as count from {table} where {where}", params)
    return int(row["count"])


def _counts_for_hashes(conn: Any, hashes: list[str]) -> dict[str, int]:
    counts = _counts()
    if hashes:
        for table in _CONTENT_TABLES:
            counts[table] = _pg_count(conn, table, "content_hash = any(%s)", (hashes,))
    return counts


def _delete_count(conn: Any, sql: str, params: tuple = ()) -> int:
    cursor = conn.execute(sql, params)
    return max(0, cursor.rowcount)


def _dedupe_pairs(
    tasks: list[tuple[ErasureTarget, str]],
) -> list[tuple[ErasureTarget, str]]:
    return list(dict.fromkeys(tasks))

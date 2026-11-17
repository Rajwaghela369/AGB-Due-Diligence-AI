"""Reference-counted deletion and retention queries (Phase 9, 9.B and 9.C).

Both implementations of `app.store.interfaces.DeletionRepo` live here, apart from the other
repositories, because deletion is the one operation that crosses every table: the memory
fake needs the internals of every memory repository, and the Postgres version is one
transaction over all of them. docs/Phase 9 API.md, "Deletion", is the contract;
docs/Phase 9 Design.md explains reference counting, the outbox and the tombstones.

Wave 0 ships stubs; the deletion slice (Wave 1) writes the bodies.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import (
    DataClass,
    DeletionReason,
    DeletionReport,
    ErasureTarget,
    ErasureTask,
    Principal,
    Tombstone,
    blob_key,
)

if TYPE_CHECKING:
    from app.store.memory import MemoryStore

# Postgres: the transaction-local setting that lets a deletion remove rows of the otherwise
# append-only `decision_events` (migration 006). Set with `set local`, so it ends with the
# transaction and no other statement ever sees it.
ERASURE_SETTING = "agb.erasure"


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
            for content_hash in orphaned:
                self.store.documents._texts.pop(content_hash, None)
            self.store.verdicts._rows = {
                key: row for key, row in self.store.verdicts._rows.items() if key[0] not in orphaned
            }
            self.store.caches._embeddings = {
                key: row
                for key, row in self.store.caches._embeddings.items()
                if key[0] not in orphaned
            }
            self.store.caches._retrievals = {
                key: row
                for key, row in self.store.caches._retrievals.items()
                if key[0] not in orphaned
            }
            self.store.caches._topics = {
                key: row for key, row in self.store.caches._topics.items() if key[0] not in orphaned
            }
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
                raise ValueError(review_id)
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

    def sweep_orphans(self) -> dict[str, int]:
        with self._lock:
            referenced = {
                clause.content_hash
                for clauses in self.store.documents._clauses.values()
                for clause in clauses
            }
            removed = _counts()
            orphaned = set(self.store.documents._texts) - referenced
            removed["clause_texts"] = len(orphaned)
            for content_hash in orphaned:
                self.store.documents._texts.pop(content_hash, None)
            for name, rows in (
                ("verdicts", self.store.verdicts._rows),
                ("embeddings", self.store.caches._embeddings),
                ("retrievals", self.store.caches._retrievals),
                ("topics", self.store.caches._topics),
            ):
                doomed = [key for key in rows if key[0] not in referenced]
                removed[name] = len(doomed)
                for key in doomed:
                    rows.pop(key, None)

            live_blobs = {
                blob_key(document) for document in self.store.documents._documents.values()
            }
            file_keys = self.store.files.keys()
            orphan_blobs = [key for key in file_keys if key not in live_blobs]
            removed["blobs"] = len(orphan_blobs)
            self.queue_erasures([("blob", key) for key in orphan_blobs])
            return removed

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
                candidates = [
                    (
                        uploaded.get(document_id)
                        or document.created_at
                        or datetime.min.replace(tzinfo=UTC),
                        document_id,
                    )
                    for document_id, document in self.store.documents._documents.items()
                ]
            elif data_class == "reviews":
                candidates = [
                    (review.finished_at, review_id)
                    for review_id, review in self.store.reviews._reviews.items()
                    if review.finished_at is not None and review.status not in ("queued", "running")
                ]
            else:
                candidates = [
                    (review.created_at or datetime.min.replace(tzinfo=UTC), review.trace_id)
                    for review in self.store.reviews._reviews.values()
                    if review.trace_id
                ]
                pending = {task.key for task in self._tasks if task.target == "trace"}
                candidates = [(at, key) for at, key in candidates if key not in pending]
            return [key for at, key in sorted(candidates) if at < before][:limit]

    def pending_erasures(self, limit: int = 100) -> list[ErasureTask]:
        with self._lock:
            rows = [task for task in self._tasks if task.done_at is None]
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
            raise ValueError(",".join(active))
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
            }
        )
        kept = {
            "clause_texts": sum(key in self.store.documents._texts for key in shared),
            "verdicts": sum(key[0] in shared for key in self.store.verdicts._rows),
            "embeddings": sum(key[0] in shared for key in self.store.caches._embeddings),
            "retrievals": sum(key[0] in shared for key in self.store.caches._retrievals),
            "topics": sum(key[0] in shared for key in self.store.caches._topics),
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
        return removed

    def _external_tasks(self, review_ids: set[str], orphaned_hashes: set[str]) -> list[ErasureTask]:
        tasks: list[ErasureTask] = []
        for review_id in sorted(review_ids):
            review = self.store.reviews._reviews[review_id]
            if review.trace_id:
                tasks.append(self._planned("trace", review.trace_id))
                tasks.append(self._planned("score", _stable_id("lawyer_agreement", review_id)))
            positions = set(self.store.reviews._clauses.get(review_id, {}))
            positions.update(pos for rid, pos in self.store.decisions._rows if rid == review_id)
            tasks.extend(
                self._planned("checkpoint", f"{review_id}:{position}")
                for position in sorted(positions)
            )
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
                    update={"task_id": self._next_task_id, "tombstone_id": tombstone_id}
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
            return self._document_report(conn, document_id, dry_run=True, lock=False)

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        with self._connect() as conn:
            report = self._document_report(conn, document_id, dry_run=False, lock=True)
            document = _pg_row(
                conn,
                "select owner_id, source_hash, source_type from documents where document_id = %s",
                (document_id,),
            )
            if document is None:  # the locked report already checked; defensive for fake DBs
                raise KeyError(document_id)
            review_ids = report.review_ids
            hashes = _pg_column(
                conn,
                "select distinct content_hash from document_clauses where document_id = %s",
                (document_id,),
            )
            orphaned, _shared = self._partition_hashes(conn, document_id, hashes)

            conn.execute(f"set local {ERASURE_SETTING} = 'on'")
            self._delete_review_rows(conn, review_ids)
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
                raise ValueError(review_id)
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

    def sweep_orphans(self) -> dict[str, int]:
        removed = _counts()
        with self._connect() as conn:
            eval_guard = self._eval_guard(conn, "candidate.content_hash")
            verdict_traces = _pg_column(
                conn,
                "select trace_id from verdicts candidate where trace_id is not null"
                " and created_at < clock_timestamp() - interval '1 hour'"
                " and not exists (select 1 from document_clauses d"
                " where d.content_hash = candidate.content_hash)" + eval_guard,
            )
            for table in ("verdicts", "embeddings", "retrievals", "topics"):
                guard = self._eval_guard(conn, "candidate.content_hash")
                removed[table] = _delete_count(
                    conn,
                    f"delete from {table} candidate"
                    " where candidate.created_at < clock_timestamp() - interval '1 hour'"
                    " and not exists (select 1 from document_clauses d"
                    " where d.content_hash = candidate.content_hash)" + guard,
                )
            guard = self._eval_guard(conn, "candidate.content_hash")
            removed["clause_texts"] = _delete_count(
                conn,
                "delete from clause_texts candidate"
                " where candidate.created_at < clock_timestamp() - interval '1 hour'"
                " and not exists (select 1 from document_clauses d"
                " where d.content_hash = candidate.content_hash)" + guard,
            )
            self._queue_conn(conn, [("trace", key) for key in verdict_traces], None)
        return removed

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

    def pending_erasures(self, limit: int = 100) -> list[ErasureTask]:
        with self._connect() as conn:
            rows = _pg_rows(
                conn,
                "select task_id, target, key, tombstone_id, requested_at, attempts,"
                " last_error, done_at from erasure_tasks where done_at is null"
                " order by requested_at, task_id limit %s",
                (limit,),
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
                cursor = conn.execute(
                    "update erasure_tasks set attempts = attempts + 1, last_error = %s"
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
    ) -> DeletionReport:
        suffix = " for update" if lock else ""
        document = _pg_row(
            conn,
            "select document_id, owner_id, source_hash, source_type from documents"
            " where document_id = %s" + suffix,
            (document_id,),
        )
        if document is None:
            raise KeyError(document_id)
        active = _pg_column(
            conn,
            "select review_id from reviews where document_id = %s"
            " and status in ('queued', 'running') order by review_id",
            (document_id,),
        )
        if active:
            raise ValueError(",".join(active))
        review_ids = _pg_column(
            conn,
            "select review_id from reviews where document_id = %s order by review_id",
            (document_id,),
        )
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
        return DeletionReport(
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
            positions = _pg_rows(
                conn,
                "select review_id, position from review_clauses where review_id = any(%s)"
                " union select review_id, position from decisions where review_id = any(%s)",
                (review_ids, review_ids),
            )
            tasks.extend(
                ("checkpoint", f"{row['review_id']}:{row['position']}") for row in positions
            )
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

    @staticmethod
    def _delete_content_rows(conn: Any, hashes: list[str], removed: dict[str, int]) -> None:
        if not hashes:
            return
        for table in ("verdicts", "embeddings", "retrievals", "topics", "clause_texts"):
            removed[table] = _delete_count(
                conn, f"delete from {table} where content_hash = any(%s)", (hashes,)
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
                " returning task_id, target, key, tombstone_id, requested_at, attempts,"
                " last_error, done_at",
                (target, key, tombstone_id),
            )
            if row is None:
                row = _pg_row(
                    conn,
                    "select task_id, target, key, tombstone_id, requested_at, attempts,"
                    " last_error, done_at from erasure_tasks"
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


_CONTENT_TABLES = ("clause_texts", "verdicts", "embeddings", "retrievals", "topics")
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

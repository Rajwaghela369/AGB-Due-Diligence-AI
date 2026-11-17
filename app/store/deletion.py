"""Reference-counted deletion and retention queries (Phase 9, 9.B and 9.C).

Both implementations of `app.store.interfaces.DeletionRepo` live here, apart from the other
repositories, because deletion is the one operation that crosses every table: the memory
fake needs the internals of every memory repository, and the Postgres version is one
transaction over all of them. docs/Phase 9 API.md, "Deletion", is the contract;
docs/Phase 9 Design.md explains reference counting, the outbox and the tombstones.

Wave 0 ships stubs; the deletion slice (Wave 1) writes the bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.domain import (
    DataClass,
    DeletionReason,
    DeletionReport,
    ErasureTarget,
    ErasureTask,
    Principal,
    Tombstone,
)

if TYPE_CHECKING:
    from app.store.memory import MemoryStore

_TODO = "Phase 9 Wave 1: deletion"

# Postgres: the transaction-local setting that lets a deletion remove rows of the otherwise
# append-only `decision_events` (migration 006). Set with `set local`, so it ends with the
# transaction and no other statement ever sees it.
ERASURE_SETTING = "agb.erasure"


class MemoryDeletionRepo:
    """The in-memory deletion, over the internals of `store`'s repositories."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def plan_document(self, document_id: str) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def delete_review(
        self, review_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def sweep_orphans(self) -> dict[str, int]:
        raise NotImplementedError(_TODO)

    def expired(self, data_class: DataClass, before: datetime, limit: int = 100) -> list[str]:
        raise NotImplementedError(_TODO)

    def pending_erasures(self, limit: int = 100) -> list[ErasureTask]:
        raise NotImplementedError(_TODO)

    def finish_erasure(self, task_id: int, error: str | None = None) -> None:
        raise NotImplementedError(_TODO)

    def queue_erasures(self, tasks: list[tuple[ErasureTarget, str]]) -> list[ErasureTask]:
        raise NotImplementedError(_TODO)

    def tombstones(self, subject_id: str | None = None) -> list[Tombstone]:
        raise NotImplementedError(_TODO)


class PostgresDeletionRepo:
    """The Postgres deletion: one transaction per document or review."""

    def __init__(self, connect: Any) -> None:
        self._connect = connect

    def plan_document(self, document_id: str) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def delete_document(
        self, document_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def delete_review(
        self, review_id: str, *, deleted_by: Principal | None, reason: DeletionReason
    ) -> DeletionReport:
        raise NotImplementedError(_TODO)

    def sweep_orphans(self) -> dict[str, int]:
        raise NotImplementedError(_TODO)

    def expired(self, data_class: DataClass, before: datetime, limit: int = 100) -> list[str]:
        raise NotImplementedError(_TODO)

    def pending_erasures(self, limit: int = 100) -> list[ErasureTask]:
        raise NotImplementedError(_TODO)

    def finish_erasure(self, task_id: int, error: str | None = None) -> None:
        raise NotImplementedError(_TODO)

    def queue_erasures(self, tasks: list[tuple[ErasureTarget, str]]) -> list[ErasureTask]:
        raise NotImplementedError(_TODO)

    def tombstones(self, subject_id: str | None = None) -> list[Tombstone]:
        raise NotImplementedError(_TODO)

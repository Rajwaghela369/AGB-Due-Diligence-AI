"""Deleting a document or review, and finishing what lives outside the database (9.B).

`Service.delete_document`, `process_erasures` and `delete_review` delegate here, so the
deletion slice (Wave 1) never edits `service.py`. The database part is
`store.deletion` (`app.store.deletion`); this module adds what the store cannot do:

- refuse while a review of the document is queued or running (`DeletionBlocked`);
- after the commit, carry out the queued `ErasureTask`s: `store.files.delete` for blobs,
  the Langfuse API for traces (`langfuse.api.trace.delete`), scores
  (`langfuse.api.scores.delete`) and dataset items, and the judge's checkpointer
  (`service.judge.forget(thread_id)`) for checkpoints, marking each with
  `store.deletion.finish_erasure`. A failure is logged and counted, never raised.

docs/Phase 9 API.md, "Deletion", is the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.logging import get_logger

from app.domain import DeletionReason, DeletionReport, Principal
from app.review.interfaces import DeletionBlocked

if TYPE_CHECKING:
    from app.review.service import Service

log = get_logger("review.deletion")


def delete_document(
    service: Service, document_id: str, principal: Principal, *, dry_run: bool = False
) -> DeletionReport:
    try:
        if dry_run:
            return service.store.deletion.plan_document(document_id)
        report = service.store.deletion.delete_document(
            document_id, deleted_by=principal, reason="user"
        )
    except ValueError as exc:
        review_ids = [value for value in str(exc).split(",") if value]
        raise DeletionBlocked(review_ids) from exc
    service.process_erasures(max(100, len(report.erasures)))
    return _refresh_tasks(service, report)


def delete_review(
    service: Service,
    review_id: str,
    principal: Principal | None,
    *,
    reason: DeletionReason = "user",
) -> DeletionReport:
    try:
        report = service.store.deletion.delete_review(
            review_id, deleted_by=principal, reason=reason
        )
    except ValueError as exc:
        raise DeletionBlocked([review_id]) from exc
    service.process_erasures(max(100, len(report.erasures)))
    return _refresh_tasks(service, report)


def process_erasures(service: Service, limit: int = 100) -> tuple[int, int]:
    done = failed = 0
    for task in service.store.deletion.pending_erasures(limit):
        try:
            _erase(service, task.target, task.key)
        except Exception as exc:  # noqa: BLE001 - the outbox exists to retain failures
            if _is_missing(exc):
                service.store.deletion.finish_erasure(task.task_id)
                done += 1
                continue
            error = f"{type(exc).__name__}: {exc}"[:2000]
            service.store.deletion.finish_erasure(task.task_id, error)
            log.warning(
                "could not erase %s %s (task %s)",
                task.target,
                task.key,
                task.task_id,
                exc_info=True,
            )
            failed += 1
        else:
            service.store.deletion.finish_erasure(task.task_id)
            done += 1
    return done, failed


def _erase(service: Service, target: str, key: str) -> None:
    if target == "blob":
        service.store.files.delete(key)
        return
    if target == "checkpoint":
        forget = getattr(service.judge, "forget", None)
        if forget is None:
            raise RuntimeError("the judge has no checkpoint eraser")
        forget(key)
        return
    if service.tracer is None:
        raise RuntimeError("Langfuse is not configured")
    api = service.tracer.api
    if target == "trace":
        api.trace.delete(key)
    elif target == "score":
        api.scores.delete(key)
    elif target == "dataset_item":
        api.dataset_items.delete(key)
    else:  # protected by ErasureTarget, but keeps corrupt outbox rows pending
        raise ValueError(f"unknown erasure target {target}")


def _is_missing(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


def _refresh_tasks(service: Service, report: DeletionReport) -> DeletionReport:
    """Reflect immediate outbox work in the response without adding a repository method."""
    pending = {task.task_id: task for task in service.store.deletion.pending_erasures(1_000_000)}
    now = datetime.now(UTC)
    refreshed = []
    for task in report.erasures:
        current = pending.get(task.task_id)
        refreshed.append(current or task.model_copy(update={"done_at": now, "last_error": None}))
    return report.model_copy(update={"erasures": refreshed})

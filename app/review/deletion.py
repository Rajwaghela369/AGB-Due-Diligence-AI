"""Deleting a document or review, and finishing what lives outside the database (9.B).

`Service.delete_document`, `process_erasures` and `delete_review` delegate here, so the
deletion slice (Wave 1) never edits `service.py`. The database part is
`store.deletion` (`app.store.deletion`); this module adds what the store cannot do:

- check that the caller owns what it deletes (a document of someone else is "unknown",
  so the API never reveals that it exists, not even through a dry run);
- refuse while a review of the document is queued or running (`DeletionBlocked`);
- after the commit, carry out the queued `ErasureTask`s: blobs through
  `store.deletion.erase_blob` (which re-checks, at that moment and under the ingest
  lock, that no document uses the key again), the Langfuse API for traces
  (`langfuse.api.trace.delete`), scores (`langfuse.api.scores.delete`) and dataset items,
  and the judge's checkpointer (`service.judge.forget("{review_id}:*")`) for checkpoints,
  marking each with `store.deletion.finish_erasure`. A failure is logged and counted,
  never raised; the task is retried after a backoff.

docs/Phase 9 API.md, "Deletion", is the contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.logging import get_logger

from app.domain import (
    ActiveReviewError,
    DeletionReason,
    DeletionReport,
    ErasureTask,
    Principal,
)
from app.review.interfaces import DeletionBlocked

if TYPE_CHECKING:
    from app.review.service import Service

log = get_logger("review.deletion")


def delete_document(
    service: Service, document_id: str, principal: Principal, *, dry_run: bool = False
) -> DeletionReport:
    _check_owner(service, document_id, principal)
    try:
        if dry_run:
            return service.store.deletion.plan_document(document_id)
        report = service.store.deletion.delete_document(
            document_id, deleted_by=principal, reason="user"
        )
    except ActiveReviewError as exc:
        raise DeletionBlocked(exc.review_ids) from exc
    # This request's own tasks first, whatever else is queued (and however much of it
    # keeps failing); the rest is the nightly job's.
    _run(service, report.erasures)
    return _refresh_tasks(service, report)


def delete_review(
    service: Service,
    review_id: str,
    principal: Principal | None,
    *,
    reason: DeletionReason = "user",
) -> DeletionReport:
    if principal is not None:
        review = service.store.reviews.get(review_id)
        if review is None or review.owner_id != principal.owner_id:
            raise KeyError(review_id)
    try:
        report = service.store.deletion.delete_review(
            review_id, deleted_by=principal, reason=reason
        )
    except ActiveReviewError as exc:
        raise DeletionBlocked(exc.review_ids) from exc
    _run(service, report.erasures)
    return _refresh_tasks(service, report)


def process_erasures(service: Service, limit: int = 100) -> tuple[int, int]:
    """Attempt up to `limit` due tasks, those whose backoff has passed, oldest due first."""
    return _run(service, service.store.deletion.pending_erasures(limit, due=True))


def _check_owner(service: Service, document_id: str, principal: Principal) -> None:
    document = service.store.documents.get(document_id)
    if document is None or document.owner_id != principal.owner_id:
        raise KeyError(document_id)


def _run(service: Service, tasks: Iterable[ErasureTask]) -> tuple[int, int]:
    done = failed = 0
    for task in tasks:
        if task.done_at is not None:
            continue
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
        if not service.store.deletion.erase_blob(key, service.store.files.delete):
            log.info("blob %s is in use again; kept", key)
        return
    if target == "checkpoint":
        forget = getattr(service.judge, "forget", None)
        if forget is None:
            if not getattr(service.judge, "interrupts", False):
                return  # a judge without a checkpointer never wrote a checkpoint
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

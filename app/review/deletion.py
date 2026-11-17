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

from typing import TYPE_CHECKING

from app.domain import DeletionReason, DeletionReport, Principal

if TYPE_CHECKING:
    from app.review.service import Service

_TODO = "Phase 9 Wave 1: deletion"


def delete_document(
    service: Service, document_id: str, principal: Principal, *, dry_run: bool = False
) -> DeletionReport:
    raise NotImplementedError(_TODO)


def delete_review(
    service: Service,
    review_id: str,
    principal: Principal | None,
    *,
    reason: DeletionReason = "user",
) -> DeletionReport:
    raise NotImplementedError(_TODO)


def process_erasures(service: Service, limit: int = 100) -> tuple[int, int]:
    raise NotImplementedError(_TODO)

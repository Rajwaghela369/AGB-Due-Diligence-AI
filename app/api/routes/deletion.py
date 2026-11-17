"""Deleting a document and everything only it used (Phase 9, 9.B). Wave 1 slice: deletion.

The route is final; the body is `Service.delete_document`, which raises
NotImplementedError (501 `not_implemented`) until Wave 1 fills it in. docs/Phase 9 API.md,
"Deletion", is the contract.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import principal, state
from app.api.errors import ApiError
from app.api.schemas import DeletionBlockedBody, DeletionOut
from app.review.interfaces import DeletionBlocked

router = APIRouter()

_BLOCKED_MESSAGE = (
    "Eine Prüfung dieses Dokuments läuft noch. Bitte warten Sie, bis sie beendet ist, und "
    "löschen Sie dann."
)


@router.delete(
    "/documents/{document_id}",
    response_model=DeletionOut,
    responses={409: {"model": DeletionBlockedBody, "description": "deletion_blocked"}},
)
def delete_document(document_id: str, request: Request, dry_run: bool = False):
    caller = principal(request)
    try:
        report = state(request).service.delete_document(document_id, caller, dry_run=dry_run)
    except KeyError as exc:
        raise ApiError(404, "not_found", "Dokument nicht gefunden.") from exc
    except DeletionBlocked as exc:
        body = DeletionBlockedBody(
            code="deletion_blocked", message=_BLOCKED_MESSAGE, review_ids=exc.review_ids
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))
    pending = sum(1 for task in report.erasures if task.done_at is None)
    return DeletionOut(report=report, pending_erasures=pending)

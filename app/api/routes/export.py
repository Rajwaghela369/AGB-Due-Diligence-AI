"""Summary block and DD export (Phase 8, 8.A and 8.B). Wave 1 slice: export.

The routes are final; the bodies are `Service.summary` and `Service.export`, which raise
NotImplementedError (501 `not_implemented`) until Wave 1 fills them in.
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.deps import reviewer, state
from app.api.errors import ApiError
from app.api.schemas import ExportBlockedBody, ReviewSummary
from app.domain import ExportFormat
from app.review.interfaces import ExportBlocked

router = APIRouter()

EXPORT_DRAFT_HEADER = "X-Export-Draft"

_BLOCKED_MESSAGE = {
    "undecided": "Offene Feststellungen: Bitte zuerst alle unwirksamen und zu prüfenden "
    "Klauseln entscheiden oder als Entwurf exportieren.",
    "not_finished": "Die Prüfung ist nicht abgeschlossen. Exportieren Sie sie als Entwurf.",
}


@router.get("/reviews/{review_id}/summary", response_model=ReviewSummary)
def get_summary(review_id: str, request: Request) -> ReviewSummary:
    try:
        return state(request).service.summary(review_id)
    except KeyError as exc:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.") from exc


@router.get(
    "/reviews/{review_id}/export",
    response_class=Response,
    responses={
        200: {"description": "The file, as an attachment."},
        409: {"model": ExportBlockedBody, "description": "export_blocked"},
    },
)
def get_export(
    review_id: str,
    request: Request,
    fmt: Annotated[ExportFormat, Query(alias="format")],
    draft: bool = False,
) -> Response:
    try:
        exported = state(request).service.export(
            review_id, fmt, draft=draft, reviewer=reviewer(request)
        )
    except KeyError as exc:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.") from exc
    except ExportBlocked as exc:
        body = ExportBlockedBody(
            code="export_blocked",
            message=_BLOCKED_MESSAGE[exc.reason],
            reason=exc.reason,
            blocking=exc.blocking,
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))
    return Response(
        content=exported.content,
        media_type=exported.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(exported.filename)}",
            EXPORT_DRAFT_HEADER: "true" if exported.draft else "false",
            "Cache-Control": "no-store",
        },
    )

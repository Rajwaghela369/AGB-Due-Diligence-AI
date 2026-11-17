"""Document ingest and immutable source-file retrieval."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.costs import cache_hit_rate, cost_eur, pricing, wall_time_ms
from app.api.deps import setting, state
from app.api.errors import ApiError
from app.api.schemas import DocumentList, DocumentListItem, DocumentOut, PasteIn
from app.ingest import IngestError, ingest_file, ingest_text

router = APIRouter()


_STATUS_BY_CODE = {
    "too_large": 413,
    "archive_too_large": 413,
    "unsupported_type": 415,
}


def _ingest_error(exc: IngestError) -> ApiError:
    return ApiError(_STATUS_BY_CODE.get(exc.code, 422), exc.code, exc.message)


@router.post("/documents", response_model=DocumentOut)
async def create_document(request: Request) -> DocumentOut:
    dependencies = state(request)
    address = request.client.host if request.client else "unknown"
    if not dependencies.rate_limiter.allow(address):
        raise ApiError(429, "rate_limited", "Zu viele Anfragen. Bitte versuchen Sie es später.")

    content_type = request.headers.get("content-type", "").lower()
    limit = int(setting(dependencies.settings, "ingest.max_upload_mb", 20)) * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise ApiError(
                    413,
                    "too_large",
                    "Die Datei überschreitet die zulässige Upload-Größe.",
                )
        except ValueError as exc:
            raise ApiError(400, "invalid_content_length", "Ungültige Content-Length.") from exc
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise ApiError(422, "invalid_request", "Das Feld 'file' fehlt.")
            data = await upload.read(limit + 1)
            if len(data) > limit:
                raise IngestError(
                    "too_large", "Die Datei überschreitet die zulässige Upload-Größe."
                )
            result = ingest_file(data, upload.filename or "Dokument", store=dependencies.store)
        elif content_type.startswith("application/json"):
            try:
                payload = PasteIn.model_validate(await request.json())
            except (ValidationError, ValueError) as exc:
                raise ApiError(422, "invalid_request", "Ungültiger eingefügter Text.") from exc
            result = ingest_text(payload.text, payload.filename, store=dependencies.store)
        else:
            raise ApiError(
                415,
                "unsupported_media_type",
                "Erwartet wird multipart/form-data oder application/json.",
            )
    except IngestError as exc:
        raise _ingest_error(exc) from exc
    # The upload page opens a reused document's stored review instead of starting another.
    latest = (
        dependencies.store.reviews.latest_for(result.document.document_id)
        if result.reused
        else None
    )
    return DocumentOut(
        document=result.document,
        clauses=result.clauses,
        reused=result.reused,
        latest_review=latest,
    )


@router.get("/documents", response_model=DocumentList)
def list_documents(request: Request) -> DocumentList:
    dependencies = state(request)
    prices = pricing(dependencies.settings)
    items = []
    for document in dependencies.store.documents.list():
        latest = dependencies.store.reviews.latest_for(document.document_id)
        item = DocumentListItem(document=document, latest_review=latest)
        if latest is not None:
            item.cache_hit_rate = cache_hit_rate(latest)
            item.cost_eur = cost_eur(latest, prices)
            item.wall_time_ms = wall_time_ms(latest)
        items.append(item)
    return DocumentList(items=items, pricing=prices)


@router.get("/documents/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, request: Request) -> DocumentOut:
    dependencies = state(request)
    document = dependencies.store.documents.get(document_id)
    if document is None:
        raise ApiError(404, "not_found", "Dokument nicht gefunden.")
    return DocumentOut(
        document=document,
        clauses=dependencies.store.documents.clauses(document_id),
        latest_review=dependencies.store.reviews.latest_for(document_id),
    )


@router.get("/documents/{document_id}/file", response_class=Response)
def get_document_file(document_id: str, request: Request) -> Response:
    """The source as stored: the PDF, or the pasted text the clause offsets index into."""
    dependencies = state(request)
    document = dependencies.store.documents.get(document_id)
    if document is None:
        raise ApiError(404, "not_found", "Dokument nicht gefunden.")
    is_text = document.source_type == "text"
    try:
        content = dependencies.store.files.get(f"{document_id}.{'txt' if is_text else 'pdf'}")
    except FileNotFoundError as exc:
        raise ApiError(404, "not_found", "Datei nicht gefunden.") from exc
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8" if is_text else "application/pdf",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{document_id}"',
        },
    )

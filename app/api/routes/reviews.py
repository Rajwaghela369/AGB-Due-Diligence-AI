"""Review lifecycle, findings, retry, and resumable event streaming."""

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.api.deps import state
from app.api.errors import ApiError
from app.api.schemas import FindingsOut, ReviewIn, ReviewOut
from app.api.sse import stream_events

router = APIRouter()


def _review_out(request: Request, review) -> ReviewOut:
    dependencies = state(request)
    total = review.counts.total
    cache_hit_rate = review.counts.cache_hits / total if total else 0.0
    limiter = getattr(dependencies, "limiter", None)
    return ReviewOut(
        review=review,
        cache_hit_rate=cache_hit_rate,
        budget_left=limiter.remaining_today() if limiter is not None else None,
        waiting_reason=limiter.waiting_reason() if limiter is not None else None,
    )


@router.post("/reviews", response_model=ReviewOut)
def create_review(payload: ReviewIn, request: Request) -> ReviewOut:
    dependencies = state(request)
    address = request.client.host if request.client else "unknown"
    if not dependencies.rate_limiter.allow(address):
        raise ApiError(429, "rate_limited", "Zu viele Anfragen. Bitte versuchen Sie es später.")
    if dependencies.store.documents.get(payload.document_id) is None:
        raise ApiError(404, "not_found", "Dokument nicht gefunden.")
    return _review_out(request, dependencies.service.start(payload.document_id, payload.scope))


@router.get("/reviews/{review_id}", response_model=ReviewOut)
def get_review(review_id: str, request: Request) -> ReviewOut:
    review = state(request).service.get(review_id)
    if review is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return _review_out(request, review)


@router.get("/reviews/{review_id}/findings", response_model=FindingsOut)
def get_findings(review_id: str, request: Request) -> FindingsOut:
    service = state(request).service
    review = service.get(review_id)
    if review is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return FindingsOut(review=review, findings=service.findings(review_id))


@router.post("/reviews/{review_id}/clauses/{position}/retry", response_model=ReviewOut)
def retry_clause(review_id: str, position: int, request: Request) -> ReviewOut:
    service = state(request).service
    if service.get(review_id) is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    try:
        service.retry_clause(review_id, position)
    except LookupError as exc:
        raise ApiError(404, "not_found", "Klausel nicht gefunden.") from exc
    except ValueError as exc:
        raise ApiError(
            409,
            "not_failed",
            "Nur fehlgeschlagene Klauseln können erneut geprüft werden.",
        ) from exc
    review = service.get(review_id)
    if review is None:  # the service contract guarantees it still exists
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return _review_out(request, review)


@router.get("/reviews/{review_id}/events", response_class=StreamingResponse)
def review_events(
    review_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    service = state(request).service
    if service.get(review_id) is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    try:
        after_seq = int(last_event_id) if last_event_id is not None else 0
        if after_seq < 0:
            raise ValueError
    except ValueError as exc:
        raise ApiError(400, "invalid_last_event_id", "Last-Event-ID muss eine Zahl sein.") from exc
    events = service.events(review_id, after_seq=after_seq, follow=True)
    return StreamingResponse(
        stream_events(request, events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

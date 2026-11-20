"""Review lifecycle, findings, retry, and resumable event streaming."""

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from app.agent.run import ExplainFailed
from app.api.costs import cache_hit_rate, cost_eur, pricing, wall_time_ms
from app.api.deps import owned_document, owned_review, state
from app.api.errors import ApiError
from app.api.schemas import Finding, FindingsOut, ReviewIn, ReviewOut
from app.api.sse import stream_events
from app.domain import Review

router = APIRouter()


def review_out(request: Request, review: Review) -> ReviewOut:
    """A review with the limiter's state, the lawyer's progress, and cost and time (see
    `ReviewOut`)."""
    dependencies = state(request)
    counts = review.counts
    prices = pricing(dependencies.settings)
    decidable = counts.potentially_void + counts.uncertain + counts.valid
    limiter = getattr(dependencies, "limiter", None)
    return ReviewOut(
        review=review,
        cache_hit_rate=cache_hit_rate(review),
        budget_left=limiter.remaining_today() if limiter is not None else None,
        waiting_reason=limiter.waiting_reason() if limiter is not None else None,
        decidable=decidable,
        progress=counts.decided / decidable if decidable else 0.0,
        can_complete=review.status in ("finished", "awaiting_decisions")
        and counts.open_void == 0
        and counts.open_review == 0,
        pricing=prices,
        cost_eur=cost_eur(review, prices),
        wall_time_ms=wall_time_ms(review),
    )


@router.post("/reviews", response_model=ReviewOut)
def create_review(payload: ReviewIn, request: Request) -> ReviewOut:
    dependencies = state(request)
    address = request.client.host if request.client else "unknown"
    if not dependencies.rate_limiter.allow(address):
        raise ApiError(429, "rate_limited", "Zu viele Anfragen. Bitte versuchen Sie es später.")
    owned_document(request, payload.document_id)
    return review_out(request, dependencies.service.start(payload.document_id, payload.scope))


@router.get("/reviews/{review_id}", response_model=ReviewOut)
def get_review(review_id: str, request: Request) -> ReviewOut:
    owned_review(request, review_id)
    review = state(request).service.get(review_id)
    if review is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return review_out(request, review)


@router.get("/reviews/{review_id}/findings", response_model=FindingsOut)
def get_findings(review_id: str, request: Request) -> FindingsOut:
    owned_review(request, review_id)
    service = state(request).service
    review = service.get(review_id)
    if review is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return FindingsOut(
        review=review,
        findings=service.findings(review_id),
        # AVV document-level findings are empty for AGB reviews.
        missing_obligations=(
            service.missing_obligations(review_id) if review.document_type == "avv" else []
        ),
    )


@router.post("/reviews/{review_id}/clauses/{position}/retry", response_model=ReviewOut)
def retry_clause(review_id: str, position: int, request: Request) -> ReviewOut:
    owned_review(request, review_id)
    service = state(request).service
    service.retry_clause(review_id, position)
    review = service.get(review_id)
    if review is None:  # the service contract guarantees it still exists
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return review_out(request, review)


@router.post("/reviews/{review_id}/clauses/{position}/explain", response_model=Finding)
def explain_clause(review_id: str, position: int, request: Request) -> Finding:
    """Regenerate a missing reasoning: one Gemini call, so it shares the review rate limit."""
    dependencies = state(request)
    address = request.client.host if request.client else "unknown"
    if not dependencies.rate_limiter.allow(address):
        raise ApiError(429, "rate_limited", "Zu viele Anfragen. Bitte versuchen Sie es später.")
    owned_review(request, review_id)
    try:
        return dependencies.service.explain_clause(review_id, position)
    except ExplainFailed as exc:
        raise ApiError(
            502, "explain_failed", "Die Begründung konnte nicht erzeugt werden."
        ) from exc


@router.get("/reviews/{review_id}/events", response_class=StreamingResponse)
def review_events(
    review_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    owned_review(request, review_id)
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

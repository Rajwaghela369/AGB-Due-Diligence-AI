"""Decision and audit routes keep HTTP concerns outside the review state machine."""

from fastapi import APIRouter, Request

from app.api.deps import owned_review, request_id, reviewer, state
from app.api.errors import ApiError
from app.api.routes.reviews import review_out
from app.api.schemas import AuditOut, DecisionIn, DecisionOut
from app.review.interfaces import InvalidDecision, NotDecidable

router = APIRouter()


@router.put(
    "/reviews/{review_id}/clauses/{position}/decision",
    response_model=DecisionOut,
)
def put_decision(
    review_id: str, position: int, payload: DecisionIn, request: Request
) -> DecisionOut:
    dependencies = state(request)
    owned_review(request, review_id)
    who = reviewer(request)
    try:
        finding = dependencies.service.decide(
            review_id,
            position,
            payload,
            who,
            request_id=request_id(request),
        )
    except KeyError as exc:
        raise ApiError(404, "not_found", "Prüfung, Klausel oder Urteil nicht gefunden.") from exc
    except NotDecidable as exc:
        raise ApiError(409, "not_decidable", str(exc)) from exc
    except InvalidDecision as exc:
        raise ApiError(422, "invalid_decision", str(exc)) from exc
    service = dependencies.service
    review = service.get(review_id)
    if review is None:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.")
    return DecisionOut(
        decision=finding.decision,
        finding=finding,
        review=review_out(request, review),
    )


@router.get("/reviews/{review_id}/audit", response_model=AuditOut)
def get_audit(
    review_id: str,
    request: Request,
    position: int | None = None,
) -> AuditOut:
    owned_review(request, review_id)
    try:
        events = state(request).service.audit(review_id, position)
    except KeyError as exc:
        raise ApiError(404, "not_found", "Prüfung nicht gefunden.") from exc
    return AuditOut(events=events)

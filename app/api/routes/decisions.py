"""A lawyer's final clause decision is an idempotent upsert, recorded in the audit trail.

Interim (Phase 7 Wave 0): still `FeedbackService.decide`; Wave 1 (decisions-api) moves it to
`ReviewService.decide` and adds `GET /reviews/{id}/audit` and `GET /metrics/agreement`, as
docs/Phase 7 API.md specifies.
"""

from fastapi import APIRouter, Request

from app.api.deps import request_id, reviewer, state
from app.api.errors import ApiError
from app.api.routes.reviews import review_out
from app.api.schemas import DecisionIn, DecisionOut

router = APIRouter()


@router.put(
    "/reviews/{review_id}/clauses/{position}/decision",
    response_model=DecisionOut,
)
def put_decision(
    review_id: str, position: int, payload: DecisionIn, request: Request
) -> DecisionOut:
    dependencies = state(request)
    who = reviewer(request)
    try:
        decision = dependencies.feedback.decide(
            review_id, position, payload, decided_by=who.name, request_id=request_id(request)
        )
    except LookupError as exc:
        raise ApiError(404, "not_found", "Prüfung, Klausel oder Urteil nicht gefunden.") from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_decision", str(exc)) from exc
    service = dependencies.service
    finding = next(f for f in service.findings(review_id) if f.position == position)
    return DecisionOut(
        decision=decision, finding=finding, review=review_out(request, service.get(review_id))
    )

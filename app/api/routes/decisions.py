"""A lawyer's final clause decision is an idempotent upsert."""

from fastapi import APIRouter, Request

from app.api.deps import state
from app.api.errors import ApiError
from app.api.schemas import DecisionIn, DecisionOut

router = APIRouter()


@router.put(
    "/reviews/{review_id}/clauses/{position}/decision",
    response_model=DecisionOut,
)
def put_decision(
    review_id: str, position: int, payload: DecisionIn, request: Request
) -> DecisionOut:
    try:
        decision = state(request).feedback.decide(review_id, position, payload)
    except LookupError as exc:
        raise ApiError(404, "not_found", "Prüfung, Klausel oder Urteil nicht gefunden.") from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_decision", str(exc)) from exc
    return DecisionOut(decision=decision)

"""Feedback endpoints expose the scoring service without leaking trace identifiers."""

from fastapi import APIRouter, Request

from app.api.deps import state
from app.api.errors import ApiError
from app.api.schemas import FeedbackIn, FeedbackOut

router = APIRouter()


@router.post("/feedback", response_model=FeedbackOut)
def submit_feedback(payload: FeedbackIn, request: Request) -> FeedbackOut:
    try:
        feedback, scored_steps = state(request).feedback.submit(payload)
    except LookupError as exc:
        raise ApiError(404, "not_found", "Prüfung, Klausel oder Urteil nicht gefunden.") from exc
    except ValueError as exc:
        raise ApiError(422, "invalid_feedback", str(exc)) from exc
    return FeedbackOut(feedback_id=feedback.feedback_id, scored_steps=scored_steps)

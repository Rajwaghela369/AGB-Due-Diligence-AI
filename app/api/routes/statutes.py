"""Serve the exact statute chunks cited by findings from the startup cache."""

from fastapi import APIRouter, Request

from app.api.deps import state
from app.api.errors import ApiError
from app.api.schemas import StatuteOut

router = APIRouter()


@router.get("/statutes/{chunk_id}", response_model=StatuteOut)
def get_statute(chunk_id: str, request: Request) -> StatuteOut:
    chunk = state(request).statutes.get(chunk_id)
    if chunk is None:
        raise ApiError(404, "not_found", "Gesetzesstelle nicht gefunden.")
    return StatuteOut.model_validate(chunk, from_attributes=True)

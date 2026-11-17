"""The per-finding rewrite chat (Phase 8, 8.C), streamed over SSE. Wave 1 slice: chat.

A POST that answers with `text/event-stream`: the browser reads it with `fetch`, since
EventSource cannot POST. Everything that can fail before the model is asked fails as an
ordinary JSON error; once streaming, a failure is a `RUN_ERROR` event
(docs/Events.md, "Rewrite chat"). Until Wave 1, `Service.chat` raises NotImplementedError
(501 `not_implemented`).
"""

import json

from core.budget import BudgetExceeded
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.api.deps import request_id, reviewer, state
from app.api.errors import ApiError
from app.api.schemas import ChatIn
from app.api.sse import stream_events
from app.domain import ChatEvent
from app.review.interfaces import NotChattable

router = APIRouter()


def encode_chat_event(event: ChatEvent) -> bytes:
    """One SSE message without `id:`: chat events are not stored and cannot be resumed."""
    data = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event.type}\ndata: {data}\n\n".encode()


@router.post(
    "/reviews/{review_id}/clauses/{position}/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def post_chat(review_id: str, position: int, payload: ChatIn, request: Request):
    """One rewrite question about one finding: one paid model call, rate-limited per IP."""
    dependencies = state(request)
    address = request.client.host if request.client else "unknown"
    if not dependencies.rate_limiter.allow(address):
        raise ApiError(429, "rate_limited", "Zu viele Anfragen. Bitte versuchen Sie es später.")
    who = reviewer(request)
    try:
        events = dependencies.service.chat(
            review_id, position, payload, who, request_id=request_id(request)
        )
    except KeyError as exc:
        raise ApiError(404, "not_found", "Prüfung oder Klausel nicht gefunden.") from exc
    except NotChattable as exc:
        raise ApiError(409, "not_chattable", str(exc)) from exc
    except BudgetExceeded as exc:
        raise ApiError(
            429, "daily_budget", "Das Tagesbudget für Modellaufrufe ist erschöpft."
        ) from exc
    return StreamingResponse(
        stream_events(request, events, encode=encode_chat_event),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

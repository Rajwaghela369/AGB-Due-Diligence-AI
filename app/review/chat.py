"""The per-finding rewrite chat (Phase 8, 8.C). Wave 1 slice: chat.

One streamed model call per message. The clause is quoted as data with the explain step's
delimiters, the answer is length-capped and carries a disclaimer, and its suggestion is
diffed against the original. docs/Events.md ("Rewrite chat") defines the event sequence;
`Service.chat` delegates here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from app.domain import ChatEvent, ChatIn, Reviewer
from app.review.interfaces import ChatModel

if TYPE_CHECKING:
    from app.review.service import Service


def start_chat(
    service: Service,
    review_id: str,
    position: int,
    message: ChatIn,
    reviewer: Reviewer,
    *,
    request_id: str | None = None,
) -> Iterator[ChatEvent]:
    """`ReviewService.chat`: validate now (raise before anything streams), then return the
    event iterator. Reads `service.findings`, `service.statutes`, `service.chat_model`,
    `service.deliverable.chat` and the prices for `ChatResult.cost_usd`."""
    raise NotImplementedError("Phase 8 Wave 1: chat")


def make_chat_model(
    config: dict[str, Any], *, limiter: Any, client: Any = None
) -> ChatModel | None:
    """The production streaming model on `models.chat`, paced by the shared limiter
    (one reservation per message). None until Wave 1 implements it, which makes the chat
    route answer 503. Wired by `app.api.main.build_app`."""
    return None

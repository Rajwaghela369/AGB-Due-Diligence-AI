"""Small dependency helpers keep route modules independent of concrete services."""

from typing import Any
from urllib.parse import unquote

from fastapi import Request

from app.api.errors import ApiError
from app.api.middleware import REQUEST_ID_SCOPE_KEY
from app.domain import Reviewer

# Who decides, until login (Phase 9). The UI sends the name the lawyer entered once,
# percent-encoded UTF-8 (`encodeURIComponent`), since header values are Latin-1 only.
REVIEWER_HEADER = "X-Reviewer"
MAX_REVIEWER_LENGTH = 100


def state(request: Request) -> Any:
    return request.app.state


def setting(settings: Any, path: str, default: Any) -> Any:
    value = settings
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part, default)
        else:
            value = getattr(value, part, default)
        if value is default:
            break
    return value


def reviewer(request: Request) -> Reviewer:
    """The unauthenticated reviewer from `X-Reviewer`; absent or blank gives `name=None`.

    Raises ApiError 422 `invalid_reviewer` for invalid percent-encoding, a name longer than
    100 characters after decoding, or one with control characters.
    """
    raw = request.headers.get(REVIEWER_HEADER)
    try:
        name = unquote(raw, errors="strict").strip() if raw is not None else ""
    except UnicodeDecodeError:
        name = None
    if (
        name is None
        or len(name) > MAX_REVIEWER_LENGTH
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
    ):
        raise ApiError(422, "invalid_reviewer", "Ungültiger Name im Feld „Prüfer“.")
    return Reviewer(name=name or None, authenticated=False)


def request_id(request: Request) -> str | None:
    """The id the request log middleware gave this request (also sent as X-Request-Id)."""
    return request.scope.get(REQUEST_ID_SCOPE_KEY)

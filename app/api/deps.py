"""Small dependency helpers keep route modules independent of concrete services."""

from typing import Any
from urllib.parse import unquote

from core.logging import get_logger
from fastapi import Request

from app.api.errors import ApiError
from app.api.middleware import REQUEST_ID_SCOPE_KEY
from app.api.oidc import InvalidToken, OidcSettings, OidcVerifier, ProviderUnavailable
from app.domain import DEFAULT_OWNER_ID, Document, Principal, Review, Reviewer, default_principal

log = get_logger("api.auth")

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
    """Who decides. With login on, the signed-in user (`authenticated=True`); `X-Reviewer`
    is ignored then, so nobody can sign a decision with someone else's name.

    With login off, the unauthenticated reviewer from `X-Reviewer`; absent or blank gives
    `name=None`.

    Raises ApiError 422 `invalid_reviewer` for invalid percent-encoding, a name longer than
    100 characters after decoding, or one with control characters.
    """
    if auth_enabled(request):
        return principal(request).reviewer()
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


_PRINCIPAL_KEY = "agb.principal"
_UNAUTHENTICATED = "Bitte melden Sie sich an."
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="agb-check"'}
_NOT_FOUND_DOCUMENT = "Dokument nicht gefunden."
_NOT_FOUND_REVIEW = "Prüfung nicht gefunden."


def auth_enabled(request: Request) -> bool:
    return bool(setting(state(request).settings, "auth.enabled", False))


def verifier(request: Request) -> OidcVerifier:
    """The process's one token verifier: injected by `create_app`, or built on first use
    from `auth.oidc`, so its key-set cache is shared by every request."""
    app_state = state(request)
    current = getattr(app_state, "oidc", None)
    if current is None:
        current = OidcVerifier(OidcSettings.from_config(app_state.settings))
        app_state.oidc = current
    return current


def principal(request: Request) -> Principal:
    """The caller (Phase 9, 9.E). With `auth.enabled` false, the configured default owner,
    unauthenticated, exactly as before login existed.

    With login on, the `Authorization: Bearer` OIDC access token, validated against
    `auth.oidc` (issuer, audience, expiry, algorithm allow-list, the issuer's JWKS); its
    claims give the principal. A missing or invalid token is 401 `unauthenticated`, and an
    unreachable provider (with no cached keys) 503. The result is kept on the request, so
    every dependency of one request sees the same caller and the token is checked once.
    """
    cached = request.scope.get(_PRINCIPAL_KEY)
    if cached is not None:
        return cached
    settings = state(request).settings
    if not setting(settings, "auth.enabled", False):
        caller = default_principal(str(setting(settings, "auth.default_owner", DEFAULT_OWNER_ID)))
    else:
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError(401, "unauthenticated", _UNAUTHENTICATED, _CHALLENGE)
        try:
            caller = verifier(request).principal(token.strip())
        except InvalidToken as exc:
            log.info("rejected a bearer token: %s", exc)
            raise ApiError(401, "unauthenticated", _UNAUTHENTICATED, _CHALLENGE) from exc
        except ProviderUnavailable as exc:
            log.warning("the identity provider is unreachable: %s", exc)
            raise ApiError(
                503, "auth_unavailable", "Die Anmeldung ist vorübergehend nicht erreichbar."
            ) from exc
    request.scope[_PRINCIPAL_KEY] = caller
    return caller


def require_login(request: Request) -> None:
    """Router-wide dependency: with login on, no route answers without a valid token."""
    principal(request)


def is_admin(request: Request, caller: Principal) -> bool:
    """The `auth.oidc.admin_role` may read and delete any owner's documents. Only an
    authenticated caller can hold it: with login off there are no roles."""
    role = setting(state(request).settings, "auth.oidc.admin_role", None)
    return bool(role) and caller.authenticated and role in caller.roles


def may_access(request: Request, owner_id: str) -> bool:
    caller = principal(request)
    return owner_id == caller.owner_id or is_admin(request, caller)


def owned_document(request: Request, document_id: str) -> Document:
    """The caller's document, or 404 `not_found`: for an unknown id and another owner's
    alike, so a response never confirms that an id exists (ids are hashes of files)."""
    document = state(request).store.documents.get(document_id)
    if document is None or not may_access(request, document.owner_id):
        raise ApiError(404, "not_found", _NOT_FOUND_DOCUMENT)
    return document


def owned_review(request: Request, review_id: str) -> Review:
    """The caller's review, or 404 `not_found` (see `owned_document`)."""
    review = state(request).store.reviews.get(review_id)
    if review is None or not may_access(request, review.owner_id):
        raise ApiError(404, "not_found", _NOT_FOUND_REVIEW)
    return review

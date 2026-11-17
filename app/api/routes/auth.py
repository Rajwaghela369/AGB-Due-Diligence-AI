"""Who the caller is (Phase 9, 9.E). Wave 2 slice: auth-scale.

While `auth.enabled` is false the answer is the configured default owner, unauthenticated,
so the UI can show "not signed in" without a login existing. docs/Phase 9 API.md, "Login".
"""

from fastapi import APIRouter, Request

from app.api.deps import principal, setting, state
from app.api.schemas import PrincipalOut

router = APIRouter()


@router.get("/me", response_model=PrincipalOut)
def me(request: Request) -> PrincipalOut:
    caller = principal(request)
    return PrincipalOut(
        owner_id=caller.owner_id,
        name=caller.name,
        roles=list(caller.roles),
        authenticated=caller.authenticated,
        auth_enabled=bool(setting(state(request).settings, "auth.enabled", False)),
    )

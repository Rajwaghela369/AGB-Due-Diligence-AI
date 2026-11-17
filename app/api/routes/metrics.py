"""Read-only quality metrics let the UI compare one immutable agent version at a time."""

from fastapi import APIRouter, Query, Request

from app.api.deps import state
from app.api.schemas import AgreementOut

router = APIRouter()


@router.get("/metrics/agreement", response_model=AgreementOut)
def get_agreement(
    request: Request, agent_version: str | None = Query(default=None)
) -> AgreementOut:
    service = state(request).service
    agreement = service.agreement(agent_version)
    return AgreementOut(
        agreement=agreement,
        current=agreement.agent_version == service.agent_version,
    )

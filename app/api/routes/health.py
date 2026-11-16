"""Liveness is unconditional; readiness reflects the injected dependency probes."""

from fastapi import APIRouter, Request, Response

from app.api.deps import state
from app.api.readiness import run_probes
from app.api.schemas import Health, Ready

router = APIRouter()


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health()


@router.get("/ready", response_model=Ready)
def ready(request: Request, response: Response) -> Ready:
    results = run_probes(state(request).readiness)
    if not all(results.values()):
        response.status_code = 503
    return Ready(ready=all(results.values()), checks=results)

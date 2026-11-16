"""Liveness is unconditional; readiness reflects injected dependency probes."""

from collections.abc import Callable

from fastapi import APIRouter, Request

from app.api.deps import setting, state
from app.api.schemas import Health, Ready

router = APIRouter()


@router.get("/health", response_model=Health)
def health() -> Health:
    return Health()


@router.get("/ready", response_model=Ready)
def ready(request: Request) -> Ready:
    checks = setting(state(request).settings, "ready_checks", {})
    results = {name: _check(probe) for name, probe in checks.items()}
    return Ready(ready=all(results.values()), checks=results)


def _check(probe: bool | Callable[[], bool]) -> bool:
    try:
        return bool(probe() if callable(probe) else probe)
    except Exception:  # noqa: BLE001 - readiness reports failure rather than failing itself
        return False

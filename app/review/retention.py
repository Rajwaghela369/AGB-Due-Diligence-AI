"""The retention policy and the nightly job (Phase 9, 9.B orphan sweep and 9.C retention).

A data class (`app.domain.DataClass`) is deleted once its period in `retention.*_days` has
passed; null keeps it until a user deletes it. Expired documents and reviews go through the
same deletion as a user's (`app.review.deletion`), so retention can never leave behind
what a user deletion would not. The job runs outside the API process: `python -m
app.review.retention` once a night (cron, a Compose service or a Kubernetes CronJob), so
two API replicas never sweep at once and a long sweep never holds a request worker.
docs/Phase 9 Design.md, "Retention", explains the periods and the choice.

Wave 0 ships the settings and stubs; the deletion slice (Wave 1) writes `run_sweep` and the
`__main__` entry point.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.domain import SweepReport

if TYPE_CHECKING:
    from app.review.service import Service


@dataclass(frozen=True)
class RetentionSettings:
    """`retention` in configs/app.yaml. A period of None keeps that class indefinitely."""

    enabled: bool = False
    files_days: int | None = None  # documents, from their last upload
    reviews_days: int | None = None  # idle reviews, from finished_at
    traces_days: int | None = None  # Langfuse traces, from their review's or verdict's start
    erasure_max_attempts: int = 10  # then the task is reported, still pending
    batch: int = 100  # ids per data class per run


def _days(value: Any) -> int | None:
    if value is None:
        return None
    days = int(value)
    if days < 1:
        raise ValueError("retention periods are whole days, at least 1 (or null)")
    return days


def retention_settings(config: Mapping[str, Any]) -> RetentionSettings:
    block = config.get("retention") or {}
    defaults = RetentionSettings()
    return RetentionSettings(
        enabled=bool(block.get("enabled", defaults.enabled)),
        files_days=_days(block.get("files_days")),
        reviews_days=_days(block.get("reviews_days")),
        traces_days=_days(block.get("traces_days")),
        erasure_max_attempts=int(block.get("erasure_max_attempts", defaults.erasure_max_attempts)),
        batch=int(block.get("batch", defaults.batch)),
    )


def run_sweep(
    service: Service, settings: RetentionSettings, now: datetime | None = None
) -> SweepReport:
    raise NotImplementedError("Phase 9 Wave 1: retention")

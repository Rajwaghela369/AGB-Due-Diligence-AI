"""The summary block (Phase 8, 8.A). Wave 1 slice: export.

`build_summary` is a pure function of the review's findings and current decisions; only
the recommendation paragraph may come from one cached model call when
`summary.llm_enabled` is set. `app.domain.ReviewSummary` and docs/Phase 8 API.md define
the result; `Service.summary` delegates here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.domain import ReviewSummary
from app.review.interfaces import SummaryModel

if TYPE_CHECKING:
    from app.review.service import Service


def build_summary(service: Service, review_id: str) -> ReviewSummary:
    """The summary of `review_id` (`ReviewService.summary`). Reads `service.store`,
    `service.findings`, `service.statutes`, `service.summary_model` and
    `service.deliverable.summary`. KeyError for an unknown review."""
    raise NotImplementedError("Phase 8 Wave 1: export (summary)")


def make_summary_model(
    config: dict[str, Any], *, limiter: Any, client: Any = None
) -> SummaryModel | None:
    """The production recommendation model (`core.llm.complete` on `models.summary`, paced
    by the shared limiter), or None when `summary.llm_enabled` is off, so nothing that
    could call a model exists then. Wired by `app.api.main.build_app`."""
    return None

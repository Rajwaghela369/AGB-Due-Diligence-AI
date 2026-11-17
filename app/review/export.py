"""The DD export (Phase 8, 8.B): memo as DOCX or Markdown, findings as CSV. Wave 1 slice:
export.

Built only from the summary, the findings, the decisions and the statute texts; it makes
no model call. docs/Phase 8 API.md defines the layouts, the blocking rule and the draft
watermark; `Service.export` delegates here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain import ExportFile, ExportFormat, Reviewer

if TYPE_CHECKING:
    from app.review.service import Service


def build_export(
    service: Service,
    review_id: str,
    fmt: ExportFormat,
    *,
    draft: bool,
    reviewer: Reviewer,
) -> ExportFile:
    """`ReviewService.export`. Raises `app.review.interfaces.ExportBlocked` for a final
    export that is not allowed yet, KeyError for an unknown review."""
    raise NotImplementedError("Phase 8 Wave 1: export")

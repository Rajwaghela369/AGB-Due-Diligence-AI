"""Request and response models for /api/v1. FastAPI's OpenAPI schema is generated from these,
and the Phase 6 client is generated from that schema, so a change here is an API change.
"""

from typing import Literal

from core.schemas import Verdict
from pydantic import BaseModel, Field

from app.domain import (
    ClauseSpan,
    Decision,
    DecisionAction,
    Document,
    FeedbackReason,
    FeedbackVote,
    Finding,
    Review,
    Topic,
)


class ErrorBody(BaseModel):
    """Every 4xx/5xx body. `code` is stable and drives the UI's message."""

    code: str  # e.g. IngestError codes, "not_found", "not_failed", "rate_limited"
    message: str


class DocumentOut(BaseModel):
    document: Document
    clauses: list[ClauseSpan]
    reused: bool = False  # the same file was uploaded before
    latest_review: Review | None = None


class PasteIn(BaseModel):
    """`POST /documents` with JSON instead of multipart, for pasted text."""

    text: str = Field(min_length=1)
    filename: str = "Eingefügter Text"


class DocumentListItem(BaseModel):
    document: Document
    latest_review: Review | None = None


class DocumentList(BaseModel):
    items: list[DocumentListItem]


class ReviewIn(BaseModel):
    document_id: str
    scope: list[Topic] = Field(default_factory=lambda: ["liability", "warranty"])


class ReviewOut(BaseModel):
    review: Review
    cache_hit_rate: float = 0.0
    budget_left: int | None = None
    waiting_reason: Literal["rate_limit", "daily_budget"] | None = None


class FindingsOut(BaseModel):
    review: Review
    findings: list[Finding]


class FeedbackIn(BaseModel):
    review_id: str
    position: int
    vote: FeedbackVote
    reasons: list[FeedbackReason] = Field(default_factory=list)  # only with "down"
    corrected_verdict: Verdict | None = None  # with "verdict_wrong"
    corrected_basis: list[str] = Field(default_factory=list)  # with "basis_wrong"
    comment: str = Field(default="", max_length=2000)


class FeedbackOut(BaseModel):
    feedback_id: str
    scored_steps: list[str]  # e.g. ["judge", "retrieval"]; "clause" for a 👍


class DecisionIn(BaseModel):
    action: DecisionAction
    corrected_verdict: Verdict | None = None
    corrected_basis: list[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2000)


class DecisionOut(BaseModel):
    decision: Decision


class StatuteOut(BaseModel):
    chunk_id: str
    citation: str
    title: str
    text: str
    source_url: str | None = None


class Health(BaseModel):
    status: Literal["ok"] = "ok"


class Ready(BaseModel):
    ready: bool
    checks: dict[str, bool]  # neon, qdrant, gemini, jina

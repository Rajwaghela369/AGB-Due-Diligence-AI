"""Request and response models for /api/v1. FastAPI's OpenAPI schema is generated from these,
and the Phase 6 client is generated from that schema, so a change here is an API change.
"""

from typing import Literal

from core.schemas import Verdict
from pydantic import BaseModel, Field

from app.domain import (
    Agreement,
    ClauseSpan,
    Decision,
    DecisionEvent,
    Document,
    ExportBlocker,
    ExportBlockReason,
    FeedbackReason,
    FeedbackVote,
    Finding,
    Review,
    Topic,
)
from app.domain import ChatIn as ChatIn  # re-exported: the POST …/chat body
from app.domain import DecisionIn as DecisionIn  # re-exported: the PUT …/decision body
from app.domain import DeletionReport as DeletionReport  # re-exported: DELETE /documents/{id}
from app.domain import ReviewSummary as ReviewSummary  # re-exported: GET …/summary


class ErrorBody(BaseModel):
    """Every 4xx/5xx body. `code` is stable and drives the UI's message."""

    code: str  # e.g. IngestError codes, "not_found", "not_failed", "rate_limited"
    message: str


class ExportBlockedBody(ErrorBody):
    """409 `export_blocked` from `GET /reviews/{id}/export` without `draft=true`."""

    reason: ExportBlockReason  # "undecided" or "not_finished"
    blocking: list[ExportBlocker] = Field(default_factory=list)  # document order


class Pricing(BaseModel):
    """The token prices of `configs/app.yaml` (`review.price_usd_per_million_tokens`,
    `review.eur_per_usd`), so the UI turns tokens into money with the server's numbers:
    `usd = (input_tokens * input_usd_per_million + output_tokens * output_usd_per_million)
    / 1e6`, and `eur = usd * eur_per_usd` when the rate is set."""

    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    eur_per_usd: float | None = None  # None: no rate configured; show USD
    # False while both prices are 0 (the placeholders): the UI shows „Preise nicht
    # hinterlegt“ instead of a cost of 0.
    confirmed: bool = False


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
    # Derived from `latest_review` like the fields of the same names on `ReviewOut`.
    cache_hit_rate: float = 0.0
    cost_eur: float | None = None
    wall_time_ms: int | None = None


class DocumentList(BaseModel):
    items: list[DocumentListItem]
    pricing: Pricing = Field(default_factory=Pricing)


class ReviewIn(BaseModel):
    document_id: str
    scope: list[Topic] = Field(default_factory=lambda: ["liability", "warranty"])


class ReviewOut(BaseModel):
    review: Review
    cache_hit_rate: float = 0.0
    budget_left: int | None = None
    waiting_reason: Literal["rate_limit", "daily_budget"] | None = None
    # Progress through the lawyer's checklist, derived from `review.counts`:
    decidable: int = 0  # findings with a verdict: potentially_void + uncertain + valid
    progress: float = 0.0  # counts.decided / decidable; 0.0 when nothing is decidable
    # „Prüfung abschließen“: no run is going, the review did not fail, and every void and
    # review finding is decided (open_void == open_review == 0).
    can_complete: bool = False
    # Cost and time (Phase 8, 8.D), derived from `review` and the configured prices:
    pricing: Pricing = Field(default_factory=Pricing)
    cost_eur: float | None = None  # review.cost_usd * eur_per_usd; None without a rate
    # finished_at - started_at once a run ended (the end of the last run: decisions do not
    # move it); now - started_at while running; None before the first start.
    wall_time_ms: int | None = None


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


# `DecisionIn` (the PUT …/decision body) lives in app.domain, since the review service takes
# it too; it is re-exported above and added to the web types by scripts/export_api_schema.py.


class DecisionOut(BaseModel):
    """`PUT /reviews/{id}/clauses/{pos}/decision`: everything the UI updates in place."""

    decision: Decision  # = finding.decision, kept for existing clients
    finding: Finding  # the clause as it now stands (never `checking`)
    review: ReviewOut  # new counts and progress


class AuditOut(BaseModel):
    """`GET /reviews/{id}/audit`: the decision history, oldest first."""

    events: list[DecisionEvent]


class AgreementOut(BaseModel):
    """`GET /metrics/agreement`: lawyer agreement for one agent version."""

    agreement: Agreement
    current: bool  # the version is the one the service judges with now


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


# --- Phase 9 (docs/Phase 9 API.md) ------------------------------------------------------


class DeletionBlockedBody(ErrorBody):
    """409 `deletion_blocked` from `DELETE /documents/{id}`: a review is still running."""

    review_ids: list[str] = Field(default_factory=list)


class DeletionOut(BaseModel):
    """`DELETE /documents/{id}`: what went (or, with `dry_run=true`, would go)."""

    report: DeletionReport
    # Erasures (blobs, traces, …) not finished within the request; the nightly sweep
    # retries them. 0 means nothing of the document is left anywhere.
    pending_erasures: int = 0


class PrincipalOut(BaseModel):
    """`GET /me`: who the server takes the caller to be."""

    owner_id: str
    name: str | None = None
    roles: list[str] = Field(default_factory=list)
    authenticated: bool  # False while login is off (`auth.enabled: false`)
    auth_enabled: bool

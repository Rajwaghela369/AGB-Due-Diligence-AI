"""Shared types for the Phase 5 service: documents, clauses, reviews, events, feedback.

The bottom layer inside `app`: every other app package may import it, it imports
nothing of ours but `core`. These shapes are the contract the parallel Phase 5
work streams meet at, so a field changes here first and everywhere else after.
"""

from datetime import datetime
from typing import Any, Literal

from core.schemas import Verdict
from pydantic import BaseModel, Field

# --- documents and clauses --------------------------------------------------

SourceType = Literal["pdf", "docx", "text"]
Topic = Literal["liability", "warranty"]


class Rect(BaseModel):
    """One line of clause text on a page, in PDF points from the top-left corner."""

    page: int  # 1-based
    x0: float
    top: float
    x1: float
    bottom: float


class ClauseSpan(BaseModel):
    """One clause as the splitter found it. Position is its 0-based index in the document."""

    position: int
    number: str | None = None  # "6.1", "§ 6", "(2)", "a)"
    heading: str | None = None  # nearest section heading, e.g. "§ 6 Haftung"
    text: str
    content_hash: str  # core.hashing.content_hash(text)
    page: int | None = None  # first page; None for pasted text
    rects: list[Rect] = Field(default_factory=list)  # empty for pasted text
    # Character offsets into the normalised document text; set for pasted text.
    start: int | None = None
    end: int | None = None
    in_scope: bool = True
    topic: str | None = None  # router hint, filled when the clause is judged


class Document(BaseModel):
    document_id: str  # sha256 of the uploaded bytes (PDF, DOCX, or the UTF-8 text)
    filename: str
    source_type: SourceType
    page_count: int | None = None
    parser_version: str
    splitter_version: str
    text_hash: str
    created_at: datetime | None = None


class IngestResult(BaseModel):
    document: Document
    clauses: list[ClauseSpan]
    reused: bool = False  # True when parse cache served it without parsing


# --- verdicts -----------------------------------------------------------------

ClauseStatus = Literal["queued", "checking", "done", "failed", "out_of_scope"]
StepName = Literal["route", "retrieve", "judge", "self-check", "retry-retrieve", "explain"]


class StepObservations(BaseModel):
    """Langfuse observation ids of the steps feedback can be scored on."""

    clause_span: str | None = None  # the per-clause `agent` span
    judge: str | None = None
    retrieval: str | None = None  # the last retrieve or retry-retrieve
    explanation: str | None = None


class ClauseVerdict(BaseModel):
    """A verdict keyed by (content_hash, agent_version); shared by every document."""

    content_hash: str
    agent_version: str
    verdict: Verdict
    legal_basis: list[str] = Field(default_factory=list)  # chunk_ids, strongest first
    candidates: list[str] = Field(default_factory=list)
    basis_scores: dict[str, float] = Field(default_factory=dict)
    reason_de: str = ""  # "" with reason_unavailable=True when explain failed
    reason_unavailable: bool = False
    confidence: float
    void_probability: float | None = None
    check: str | None = None  # which self-check rule fired
    trace_id: str | None = None
    observations: StepObservations = Field(default_factory=StepObservations)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None


# --- reviews ------------------------------------------------------------------

ReviewStatus = Literal["queued", "running", "finished", "failed"]


class ReviewCounts(BaseModel):
    total: int = 0  # in-scope clauses
    done: int = 0
    failed: int = 0
    potentially_void: int = 0
    uncertain: int = 0
    valid: int = 0
    out_of_scope: int = 0
    cache_hits: int = 0


class Review(BaseModel):
    review_id: str
    document_id: str
    agent_version: str
    scope: list[Topic]
    status: ReviewStatus
    counts: ReviewCounts = Field(default_factory=ReviewCounts)
    cost_usd: float = 0.0
    trace_id: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


DecisionAction = Literal["accept", "override", "dismiss"]


class Decision(BaseModel):
    review_id: str
    position: int
    action: DecisionAction
    corrected_verdict: Verdict | None = None
    corrected_basis: list[str] = Field(default_factory=list)
    note: str = ""
    decided_by: str | None = None  # no login in v1
    decided_at: datetime | None = None


class Finding(BaseModel):
    """One clause's result inside one review: the reload path for the UI."""

    position: int
    clause: ClauseSpan
    status: ClauseStatus
    verdict: ClauseVerdict | None = None
    error: str | None = None
    decision: Decision | None = None
    feedback: "Feedback | None" = None  # the latest vote on this clause


# --- feedback -----------------------------------------------------------------

FeedbackVote = Literal["up", "down"]
FeedbackReason = Literal["verdict_wrong", "basis_wrong", "reasoning_unclear", "clause_cut"]

# Which step each reason is scored on (Phase 5 plan, 5.E). `None` means no trace
# step: stored and reported separately. A 👍 is scored on the clause span.
REASON_STEP: dict[FeedbackReason, str | None] = {
    "verdict_wrong": "judge",
    "basis_wrong": "retrieval",
    "reasoning_unclear": "explanation",
    "clause_cut": None,
}


class Feedback(BaseModel):
    feedback_id: str  # stable: same review, clause and author → same id, so a vote updates
    review_id: str
    position: int
    agent_version: str
    vote: FeedbackVote
    reasons: list[FeedbackReason] = Field(default_factory=list)
    corrected_verdict: Verdict | None = None
    corrected_basis: list[str] = Field(default_factory=list)
    comment: str = ""
    source: Literal["vote", "decision"] = "vote"
    # Langfuse observation id per scored step ("judge", "retrieval", "explanation",
    # "clause"), filled when the scores are written; the audit trail of where each went.
    observations: dict[str, str | None] = Field(default_factory=dict)
    created_at: datetime | None = None


Finding.model_rebuild()


# --- events -------------------------------------------------------------------

EVENT_SCHEMA_VERSION = 1

# AG-UI event types the service emits, in `ag_ui.core.EventType` spelling.
EventType = Literal[
    "RUN_STARTED",
    "STATE_SNAPSHOT",
    "STEP_STARTED",
    "STEP_FINISHED",
    "STATE_DELTA",
    "CUSTOM",
    "RUN_ERROR",
    "RUN_FINISHED",
]

# `CUSTOM` event names.
CUSTOM_PROGRESS = "progress"  # value: ReviewCounts + cost_usd + budget_left + waiting_reason


class StoredEvent(BaseModel):
    """One AG-UI event as persisted in `run_events` and sent as one SSE message.

    `seq` starts at 1 per review and is the SSE `id`, so `Last-Event-ID: n`
    resumes at `seq > n`. `payload` is the AG-UI event serialised with
    `model_dump(by_alias=True, exclude_none=True)` plus `schemaVersion`.
    """

    review_id: str
    seq: int
    type: EventType
    payload: dict[str, Any]
    created_at: datetime | None = None

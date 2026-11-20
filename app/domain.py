"""Shared domain types for documents, clauses, reviews, events, and feedback.

The bottom layer inside `app`: every other app package may import it, it imports
nothing of ours but `core`. These shapes are the contract the parallel Phase 5
work streams meet at, so a field changes here first and everywhere else after.
"""

import hashlib
from datetime import datetime
from typing import Any, Literal

from core.schemas import Verdict
from pydantic import BaseModel, Field, model_validator

# --- documents and clauses --------------------------------------------------

# Default owner for rows written without a login and for legacy rows.
# existed before migration 006. `auth.default_owner` in configs/app.yaml overrides it.
DEFAULT_OWNER_ID = "local"

SourceType = Literal["pdf", "docx", "text"]
DocumentType = Literal["agb", "avv"]
Topic = Literal["liability", "warranty"]
ObligationCode = Literal["a", "b", "c", "d", "e", "f", "g", "h"]


class Obligation(BaseModel):
    """One mandatory term of Art. 28(3)(a)-(h) DSGVO in the AVV profile."""

    code: ObligationCode
    label_de: str
    question_de: str
    legal_basis: str


class ObligationFinding(BaseModel):
    """The AVV agent's answer for one obligation mentioned by one clause."""

    obligation: ObligationCode
    fulfilled: bool
    confidence: float = Field(ge=0.0, le=1.0)
    legal_basis: list[str] = Field(default_factory=list)
    reason_de: str = ""
    reason_unavailable: bool = False


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
    # Router topic ("liability", "warranty", "other"). Ingest leaves it None so it stays
    # offline and deterministic; a review tags its clauses at start and stores the topic on
    # its own clause rows, and every clause the review returns carries it.
    topic: str | None = None


class Document(BaseModel):
    document_id: str  # sha256 of the uploaded bytes (PDF, DOCX, or the UTF-8 text)
    filename: str
    source_type: SourceType
    page_count: int | None = None
    parser_version: str
    splitter_version: str
    text_hash: str
    # Chosen once at upload. Existing documents and clients remain AGB reviews.
    document_type: DocumentType = "agb"
    created_at: datetime | None = None
    # Owner of the document. Before login every document belongs to the configured
    # default owner (`auth.default_owner`, DEFAULT_OWNER_ID); migration 006 gives existing
    # rows that owner.
    owner_id: str = Field(default_factory=lambda: DEFAULT_OWNER_ID)
    # sha256 of the uploaded bytes: the stem of the stored file's key (`blob_key`). Equal
    # to `document_id` for the default owner; another owner's document of the same bytes
    # has its own id (`document_id_for`) but shares the blob, which deletion therefore
    # reference-counts. None on construction means "same as document_id".
    source_hash: str | None = None

    @model_validator(mode="after")
    def _default_source_hash(self) -> "Document":
        if self.source_hash is None:
            self.source_hash = self.document_id
        return self


class IngestResult(BaseModel):
    document: Document
    clauses: list[ClauseSpan]
    reused: bool = False  # True when parse cache served it without parsing


# --- verdicts -----------------------------------------------------------------

# queued → checking → done | failed | awaiting_decision; a cache hit goes queued → done, or
# queued → awaiting_decision for a cached `uncertain` verdict. awaiting_decision → done when
# the lawyer decides. A clause has a verdict in `done` and `awaiting_decision` (and may have
# one in `checking`); never in `queued`, `failed` or `out_of_scope`.
ClauseStatus = Literal["queued", "checking", "done", "failed", "out_of_scope", "awaiting_decision"]
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
    # Empty for AGB. An AVV clause may answer more than one Art. 28 obligation.
    obligation_findings: list[ObligationFinding] = Field(default_factory=list)


# --- reviews ------------------------------------------------------------------

# queued → running → finished | awaiting_decisions | failed. A run ends `awaiting_decisions`
# when at least one clause is `awaiting_decision`; the decision that settles the last one
# makes it `finished` (when no run is going). A retry makes any idle review `running`
# again; a decision never changes `finished` or `failed`. See docs/Events.md.
ReviewStatus = Literal["queued", "running", "finished", "awaiting_decisions", "failed"]

# Statuses in which no run of the review is going: its event log ends in a terminal event.
IDLE_REVIEW_STATUSES: tuple[ReviewStatus, ...] = ("finished", "awaiting_decisions", "failed")


class ReviewCounts(BaseModel):
    """Per-review tallies, computed by `app.review.runner.count` and nowhere else.

    The verdict classes count the model's verdict of every clause that has one (`done` or
    `awaiting_decision`). The decision tallies count current decisions (one per clause)
    on clauses with a verdict; `open_*` are those verdicts with no decision yet. The
    review can be closed („Prüfung abschließen“) when `open_void == open_review == 0`.
    """

    total: int = 0  # in-scope clauses
    done: int = 0  # status `done`
    failed: int = 0
    awaiting_decision: int = 0  # status `awaiting_decision`
    potentially_void: int = 0
    uncertain: int = 0
    valid: int = 0
    out_of_scope: int = 0
    cache_hits: int = 0
    decided: int = 0  # clauses with a verdict and a decision (any action)
    open_void: int = 0  # potentially_void verdicts without a decision
    open_review: int = 0  # uncertain verdicts without a decision


class Review(BaseModel):
    review_id: str
    document_id: str
    agent_version: str
    # Copied from the document when the review is created; clients never choose it here.
    document_type: DocumentType = "agb"
    scope: list[Topic]
    # Empty for AGB; AVV reviews normally carry all letters a-h.
    obligations: list[ObligationCode] = Field(default_factory=list)
    status: ReviewStatus
    counts: ReviewCounts = Field(default_factory=ReviewCounts)
    cost_usd: float = 0.0
    trace_id: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # User who started the review; initially the document owner.
    owner_id: str = Field(default_factory=lambda: DEFAULT_OWNER_ID)


# --- decisions ------------------------------------------------------------------

DecisionAction = Literal["accept", "override", "dismiss"]
FindingStatus = Literal["open", "accepted", "overridden", "dismissed"]

FINDING_STATUS: dict[DecisionAction, FindingStatus] = {
    "accept": "accepted",
    "override": "overridden",
    "dismiss": "dismissed",
}


class Reviewer(BaseModel):
    """Who decides. Before login (Phase 9) the name comes from the `X-Reviewer` header and is
    not authenticated; the audit trail says so."""

    name: str | None = None  # None: the header was absent or blank
    authenticated: bool = False


class DecisionIn(BaseModel):
    """A lawyer's decision on one finding. `corrected_*` are only allowed with `override`;
    an override needs `corrected_verdict`, and an empty `corrected_basis` keeps the model's §."""

    action: DecisionAction
    corrected_verdict: Verdict | None = None
    corrected_basis: list[str] = Field(default_factory=list)  # statute chunk_ids
    note: str = Field(default="", max_length=2000)


class Decision(BaseModel):
    """The current decision on one clause of one review (one row per review and position)."""

    review_id: str
    position: int
    action: DecisionAction
    corrected_verdict: Verdict | None = None
    corrected_basis: list[str] = Field(default_factory=list)
    note: str = ""
    decided_by: str | None = None  # Reviewer.name
    decided_at: datetime | None = None
    # The model's verdict and basis when the decision was made, so the history and the
    # agreement figures never need the shared verdict cache.
    model_verdict: Verdict | None = None
    model_basis: list[str] = Field(default_factory=list)
    # The reasoning written for the lawyer's verdict when a decision resumes an interrupted
    # clause and the decided verdict is potentially_void (Phase 7 plan, 7.B). The verdict
    # row keeps the model's own reasoning.
    reason_de: str = ""
    reason_unavailable: bool = False


def decided_verdict(decision: Decision) -> tuple[Verdict | None, list[str]]:
    """The verdict and basis in force after a decision: the model's on accept, the corrected
    ones on override (an empty corrected basis keeps the model's), none on dismiss."""
    if decision.action == "accept":
        return decision.model_verdict, list(decision.model_basis)
    if decision.action == "override":
        return decision.corrected_verdict, list(decision.corrected_basis or decision.model_basis)
    return None, []


def kept_model_verdict(decision: Decision) -> bool | None:
    """True if the lawyer kept the model's verdict (accept, or an override of the § only),
    False if they changed it, None for a dismissal (not counted as agreement or not)."""
    if decision.action == "dismiss":
        return None
    return decided_verdict(decision)[0] == decision.model_verdict


class DecisionEvent(BaseModel):
    """One row of the append-only decision history (`decision_events`).

    Written in the same transaction as the decision it records, never updated or deleted.
    `from_*` is what was in force before (the previous decision's `decided_verdict`, or the
    model's verdict and basis for the first decision), `to_*` what is in force after.
    """

    event_id: int  # assigned by the store, increasing in write order
    review_id: str
    position: int
    action: DecisionAction
    previous_action: DecisionAction | None = None  # None for the first decision
    from_verdict: Verdict | None = None
    from_basis: list[str] = Field(default_factory=list)
    to_verdict: Verdict | None = None  # None after a dismissal
    to_basis: list[str] = Field(default_factory=list)
    note: str = ""
    decided_by: str | None = None
    authenticated: bool = False
    agent_version: str
    request_id: str | None = None  # the API request's X-Request-Id
    at: datetime


class AgreementCounts(BaseModel):
    """Lawyer agreement over current decisions; `decided = kept + overridden + dismissed`."""

    decided: int = 0
    kept: int = 0  # accepted, or overridden with the same verdict (§ changed only)
    overridden: int = 0  # overridden to a different verdict
    dismissed: int = 0  # not relevant: neither agreement nor disagreement
    rate: float | None = None  # kept / (kept + overridden); None when that is 0


class AgreementByVerdict(BaseModel):
    """The same figures per model verdict: `potentially_void.rate` is void precision as the
    lawyer sees it."""

    potentially_void: AgreementCounts = Field(default_factory=AgreementCounts)
    uncertain: AgreementCounts = Field(default_factory=AgreementCounts)
    valid: AgreementCounts = Field(default_factory=AgreementCounts)


class Agreement(AgreementCounts):
    """Lawyer agreement for one agent version, over every review judged by it."""

    agent_version: str
    by_verdict: AgreementByVerdict = Field(default_factory=AgreementByVerdict)
    last_decided_at: datetime | None = None


def agreement_rate(kept: int, overridden: int) -> float | None:
    return kept / (kept + overridden) if kept + overridden else None


class Finding(BaseModel):
    """One clause's result inside one review: the reload path for the UI."""

    position: int
    clause: ClauseSpan
    status: ClauseStatus
    verdict: ClauseVerdict | None = None
    error: str | None = None
    decision: Decision | None = None
    # Derived from `decision` on construction: "open" when there is none.
    finding_status: FindingStatus = "open"
    feedback: "Feedback | None" = None  # the latest vote on this clause
    # Empty for AGB; copied from the clause verdict for AVV presentation and reloads.
    obligation_findings: list[ObligationFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_finding_status(self) -> "Finding":
        self.finding_status = (
            FINDING_STATUS[self.decision.action] if self.decision is not None else "open"
        )
        return self


class MissingObligation(BaseModel):
    """A document-level AVV finding: no clause mentions one mandatory obligation."""

    review_id: str
    obligation: Obligation
    reason_de: str
    finding_status: FindingStatus = "open"


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

EVENT_SCHEMA_VERSION = 2

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
CUSTOM_INTERRUPT = "interrupt"  # value: Interrupt

# Shown to the lawyer on a clause awaiting a decision.
INTERRUPT_QUESTION = (
    "Das Modell ist unsicher. Bitte entscheiden Sie: Ergebnis übernehmen, ändern oder als "
    "nicht relevant verwerfen."
)


class Interrupt(BaseModel):
    """`CUSTOM` `interrupt` value: a clause now waits for the lawyer (docs/Events.md)."""

    position: int
    verdict: Verdict  # the model's verdict; "uncertain" in v1
    confidence: float
    question: str = INTERRUPT_QUESTION


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


# --- Phase 8: summary, export and rewrite chat ---------------------------------
#
# docs/Phase 8 API.md is the prose contract for everything below. The summary is a pure
# function of the review's findings and decisions; only its optional recommendation
# paragraph may come from a model (`summary.llm_enabled`, off by default).

# Where a void finding has no § in force, it is grouped under this label.
SUMMARY_NO_BASIS = "ohne Norm"


class SummaryFinding(BaseModel):
    """One finding as the summary and the export see it: the verdict *in force*.

    In force means `decided_verdict(decision)` once the lawyer decided, else the model's
    verdict (`finding_status == "open"`). A dismissed finding has no verdict in force and
    never appears in a summary list; it is only counted.
    """

    position: int
    number: str | None = None  # ClauseSpan.number, e.g. "6.1"
    heading: str | None = None  # ClauseSpan.heading
    excerpt: str  # the clause text, cut to `summary.excerpt_chars` at a word boundary
    verdict: Verdict  # in force
    basis: list[str] = Field(default_factory=list)  # chunk_ids in force, strongest first
    citations: list[str] = Field(default_factory=list)  # LawChunk.citation of each basis id
    model_verdict: Verdict
    confidence: float  # the model's (ClauseVerdict.confidence)
    finding_status: FindingStatus
    # An override from a non-void model verdict to potentially_void: the lawyer found a
    # risk the model missed, so it ranks with the confirmed void findings.
    overridden_to_void: bool = False
    decided_by: str | None = None
    note: str = ""


class SummaryGroup(BaseModel):
    """Void findings that share the statute paragraph of their strongest § in force.

    The paragraph is that of the first chunk of `SummaryFinding.basis` (`LawChunk.law` and
    `LawChunk.paragraph`), so § 309 Nr. 7 and § 309 Nr. 8 fall in one group "§ 309 BGB".
    Findings without a basis form one group, `key = label = SUMMARY_NO_BASIS`.
    """

    key: str  # stable, e.g. "bgb-309"; SUMMARY_NO_BASIS for findings without a §
    label: str  # shown, e.g. "§ 309 BGB"
    findings: list[SummaryFinding]  # document order


class SummaryCounts(BaseModel):
    """What the summary block and the memo cover state, by the verdict in force."""

    findings: int = 0  # clauses with a verdict (ReviewOut.decidable)
    potentially_void: int = 0  # in force, decided or still open
    uncertain: int = 0
    valid: int = 0
    accepted: int = 0
    overridden: int = 0
    dismissed: int = 0
    open: int = 0  # findings without a decision
    open_void: int = 0  # = ReviewCounts.open_void: block a final export
    open_review: int = 0  # = ReviewCounts.open_review: block a final export
    failed: int = 0  # clauses that could not be judged (listed as „nicht geprüft“)
    out_of_scope: int = 0


class ReviewSummary(BaseModel):
    """The summary block (8.A): deterministic from the findings and the current decisions.

    `decision_set_hash` changes exactly when anything the deterministic part shows changes
    (a decision, a note, a verdict); it keys the cached LLM recommendation, so the paragraph
    is generated at most once per decision set. With `summary.llm_enabled` false (the
    default) the recommendation is always the rule-based German text.
    """

    review_id: str
    document_id: str
    filename: str
    agent_version: str
    review_status: ReviewStatus
    # True while a final export would be blocked: open void or review findings, or the
    # review is not idle-and-successful (`status` not in finished / awaiting_decisions).
    draft: bool
    counts: SummaryCounts
    # Groups ordered by size (largest first), then by `key`.
    void_groups: list[SummaryGroup] = Field(default_factory=list)
    # At most `summary.top_risks` findings whose verdict in force is potentially_void or
    # uncertain. Order: lawyer-confirmed void (accepted void, or overridden_to_void) first,
    # then open void, then uncertain; within each by model confidence (descending), then
    # by position.
    top_risks: list[SummaryFinding] = Field(default_factory=list)
    recommendation: str  # German, one paragraph
    recommendation_from_llm: bool = False  # False: the rule-based text (also on LLM failure)
    decision_set_hash: str  # sha256 hex
    reviewers: list[str] = Field(default_factory=list)  # distinct Decision.decided_by, sorted
    last_decided_at: datetime | None = None
    generated_at: datetime


ExportFormat = Literal["docx", "md", "csv"]

EXPORT_MEDIA_TYPES: dict[ExportFormat, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}

# The watermark of a draft export: diagonal on every DOCX page, a banner line in Markdown,
# and the `entwurf` column in CSV.
DRAFT_WATERMARK = "Entwurf"

EXPORT_DISCLAIMER = (
    "Dieses Dokument wurde mit Unterstützung eines KI-Systems erstellt. Maßgeblich sind "
    "allein die Entscheidungen der prüfenden Person. Es ersetzt keine Rechtsberatung."
)

ExportBlockReason = Literal["undecided", "not_finished"]


class ExportBlocker(BaseModel):
    """One finding that blocks a final export: a void or review verdict without a decision."""

    position: int
    number: str | None = None
    heading: str | None = None
    verdict: Verdict  # the model's: potentially_void or uncertain


class ExportFile(BaseModel):
    """A rendered export, as the service hands it to the API."""

    filename: str  # e.g. "AGB-Check_Muster-AGB_2026-11-17_Entwurf.docx"; no path
    media_type: str  # EXPORT_MEDIA_TYPES[format]
    content: bytes
    draft: bool


# Shown under every rewrite suggestion and stored in its result.
CHAT_DISCLAIMER = (
    "KI-generierter Formulierungsvorschlag, keine Rechtsberatung. Bitte vor Verwendung "
    "anwaltlich prüfen."
)

ChatRole = Literal["user", "assistant"]


class ChatMessage(BaseModel):
    """One earlier turn of a finding's chat. The server keeps no chat history: the client
    sends it back, and the server treats all of it as untrusted data."""

    role: ChatRole
    content: str = Field(max_length=8000)


class ChatIn(BaseModel):
    """`POST …/clauses/{position}/chat`: one question about one finding."""

    message: str = Field(min_length=1, max_length=2000)
    # Oldest first; the server uses only the last `chat.max_history_messages`.
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


DiffOp = Literal["equal", "insert", "delete"]


class DiffSegment(BaseModel):
    """One run of a word-level diff from the original clause to the suggestion.
    Concatenating the `equal` and `delete` texts gives the original, `equal` and `insert`
    the suggestion; whitespace belongs to the segment it follows."""

    op: DiffOp
    text: str


class ChatResult(BaseModel):
    """`CUSTOM chat_result` value: the finished answer, once streaming is over."""

    message_id: str
    text: str  # the whole answer as plain text, at most `chat.max_answer_chars`
    suggestion: str | None = None  # the proposed clause wording, if the answer has one
    diff: list[DiffSegment] = Field(default_factory=list)  # [] without a suggestion
    truncated: bool = False  # the answer hit the length cap (tokens or characters)
    disclaimer: str = CHAT_DISCLAIMER
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0  # at the configured prices; not added to Review.cost_usd
    latency_ms: int = 0


# `CUSTOM` event name of the chat stream (docs/Events.md, "Rewrite chat").
CUSTOM_CHAT_RESULT = "chat_result"  # value: ChatResult

ChatEventType = Literal[
    "RUN_STARTED",
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_END",
    "CUSTOM",
    "RUN_ERROR",
    "RUN_FINISHED",
]


class ChatEvent(BaseModel):
    """One event of a chat stream: an AG-UI event serialised like a review event
    (`app.review.events.serialise`), sent as one SSE message without an `id:`. Chat events
    are not stored and a chat stream cannot be resumed."""

    type: ChatEventType
    payload: dict[str, Any]


# --- Phase 9: owners, deletion and retention ------------------------------------
#
# docs/Phase 9 API.md is the prose contract, docs/Phase 9 Design.md the reasoning.


class Principal(BaseModel):
    """Who is calling: the OIDC subject of the session (9.E), or the default owner while
    login is off (`auth.enabled: false`).

    Provider-agnostic: only standard OIDC claims are read (`sub`, `iss`, `name`, and the
    configured roles claim), so Keycloak in development and any production provider fill it
    the same way. The e-mail address is deliberately not kept: nothing needs it.
    """

    subject: str  # the `sub` claim; stable per issuer, never reassigned
    issuer: str | None = None  # the `iss` claim; None for the default owner
    name: str | None = None  # the `name` claim, shown in the audit trail
    roles: list[str] = Field(default_factory=list)
    authenticated: bool = False  # False: login is off, the caller is the default owner

    @property
    def owner_id(self) -> str:
        """The `owner_id` written on documents and reviews: the subject. One issuer per
        deployment (decision #2), so the subject alone is unique."""
        return self.subject

    def reviewer(self) -> Reviewer:
        """The audit trail's "who", from the session instead of the `X-Reviewer` header."""
        return Reviewer(name=self.name or self.subject, authenticated=self.authenticated)


def default_principal(owner_id: str = DEFAULT_OWNER_ID) -> Principal:
    """The caller while login is off: the default owner, unauthenticated."""
    return Principal(subject=owner_id, authenticated=False)


def document_id_for(
    source_hash: str,
    owner_id: str = DEFAULT_OWNER_ID,
    document_type: DocumentType = "agb",
) -> str:
    """A document's id: per owner, so two users uploading the same file get two documents
    and never see each other's reviews. The default owner keeps the plain source hash, so
    every id (and URL) from before Phase 9 stays valid."""
    if owner_id == DEFAULT_OWNER_ID and document_type == "agb":
        return source_hash
    if document_type == "agb":
        return hashlib.sha256(f"{owner_id}\x00{source_hash}".encode()).hexdigest()
    return hashlib.sha256(f"{owner_id}\x00{document_type}\x00{source_hash}".encode()).hexdigest()


def blob_key(document: Document) -> str:
    """The FileStore key of a document's stored file: the PDF (also for DOCX, converted) or
    the pasted text. Keyed by the bytes, so documents of the same file share one blob."""
    suffix = "txt" if document.source_type == "text" else "pdf"
    return f"{document.source_hash or document.document_id}.{suffix}"


# Retention classes (9.C): each has its own period in `retention.*_days`.
DataClass = Literal["files", "reviews", "traces"]

# Why something was deleted; recorded on its tombstone.
DeletionReason = Literal["user", "retention", "orphan_sweep"]

# What cannot be deleted inside the database transaction, so the transaction queues it
# (`erasure_tasks`, an outbox) and a worker finishes it afterwards, retrying until done:
#   blob          a FileStore key (`blob_key`)
#   trace         a Langfuse trace id (a review's trace, or an unshared verdict's)
#   score         a Langfuse score id (feedback on a shared trace that itself stays)
#   dataset_item  a Langfuse dataset item id (a lawyer correction, `corrections`)
#   checkpoint    every LangGraph checkpoint thread of one review: `checkpoint_key(review_id)`,
#                 i.e. `{review_id}:*`, matching the threads `{review_id}:{position}`
ErasureTarget = Literal["blob", "trace", "score", "dataset_item", "checkpoint"]


def checkpoint_key(review_id: str) -> str:
    """The erasure key that names all checkpoint threads of a review (`{review_id}:*`)."""
    return f"{review_id}:*"


class ActiveReviewError(ValueError):
    """Raised by `DeletionRepo` while a review of the document (or the review itself) is
    queued or running. A dedicated type, so a validation error inside the repository is
    never mistaken for "blocked" (the API maps only this one to 409)."""

    def __init__(self, review_ids: list[str]) -> None:
        super().__init__(",".join(review_ids))
        self.review_ids = list(review_ids)


# The row counts of a deletion, by table, in this order. `kept_shared` uses the same keys
# for content-addressed rows another document still references.
DELETION_TABLES: tuple[str, ...] = (
    "documents",
    "uploads",
    "document_clauses",
    "reviews",
    "review_clauses",
    "run_events",
    "decisions",
    "decision_events",
    "feedback",
    "clause_texts",
    "verdicts",
    "embeddings",
    "retrievals",
    "topics",
    "obligation_routes",
    "summary_texts",
)


class ErasureTask(BaseModel):
    """One outbox row: something outside the database to delete after the commit."""

    task_id: int  # assigned by the store
    target: ErasureTarget
    key: str
    tombstone_id: str | None = None  # the deletion that queued it; None for a sweep
    requested_at: datetime
    attempts: int = 0
    last_error: str | None = None
    done_at: datetime | None = None
    # A failed task waits (exponential backoff) before it is due again, so failing tasks
    # never block newer ones at the head of the queue. None: due at once.
    next_attempt_at: datetime | None = None


class Tombstone(BaseModel):
    """What the audit trail keeps of a deletion: who deleted what, when and why, and how
    many rows went; no content (no filename, no clause text, no note). Append-only."""

    tombstone_id: str
    kind: Literal["document", "review", "sweep"]
    subject_id: str  # the document or review id (a hash, not content); "orphans" for a sweep
    owner_id: str
    deleted_by: str | None = None  # Principal.owner_id; None for retention and sweeps
    authenticated: bool = False
    reason: DeletionReason
    removed: dict[str, int] = Field(default_factory=dict)  # DELETION_TABLES keys
    at: datetime


class DeletionReport(BaseModel):
    """What a deletion removed (or, with `dry_run`, would remove)."""

    document_id: str | None = None  # None when a single review was deleted
    review_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False
    removed: dict[str, int] = Field(default_factory=dict)  # DELETION_TABLES keys
    # Content-addressed rows the document used that stay, because another document (or the
    # evaluation `clauses` table) still references them.
    kept_shared: dict[str, int] = Field(default_factory=dict)
    # Queued for after the commit (blobs, traces, scores, dataset items, checkpoints);
    # `done_at` is set on those the request itself already finished.
    erasures: list[ErasureTask] = Field(default_factory=list)
    tombstone: Tombstone | None = None  # None on a dry run


class SweepReport(BaseModel):
    """One run of the nightly job (9.B orphan sweep and 9.C retention)."""

    started_at: datetime
    finished_at: datetime
    expired_documents: int = 0  # deleted through 9.B because `files_days` passed
    expired_reviews: int = 0  # deleted because `reviews_days` passed
    expired_traces: int = 0  # Langfuse traces queued because `traces_days` passed
    orphans_removed: dict[str, int] = Field(default_factory=dict)  # DELETION_TABLES keys
    erasures_done: int = 0
    erasures_failed: int = 0  # still pending; retried next run

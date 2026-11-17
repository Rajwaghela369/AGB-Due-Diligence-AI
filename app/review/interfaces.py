"""What the API asks of the review layer. The runner implements it; the API depends only on this."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from core.llm import Completion

from app.domain import (
    Agreement,
    ChatEvent,
    ChatIn,
    DecisionEvent,
    DecisionIn,
    DeletionReport,
    ExportBlocker,
    ExportBlockReason,
    ExportFile,
    ExportFormat,
    Finding,
    Principal,
    Review,
    Reviewer,
    ReviewSummary,
    StoredEvent,
    SweepReport,
    Topic,
)


class InvalidDecision(ValueError):
    """The decision itself is malformed (API: 422 `invalid_decision`), as opposed to a plain
    ValueError, which means the clause's state forbids it (409)."""


class NotDecidable(ValueError):
    """The clause has no verdict in this review (`queued`, `checking`, `failed`,
    `out_of_scope`), so there is nothing to decide (API: 409 `not_decidable`)."""


class ExportBlocked(ValueError):
    """A final (non-draft) export is not allowed yet (API: 409 `export_blocked`).

    `reason="undecided"`: `blocking` lists every void or review finding without a decision,
    in document order. `reason="not_finished"`: the review is queued, running or failed;
    `blocking` is empty. A draft export (`draft=True`) is never blocked.
    """

    def __init__(self, reason: ExportBlockReason, blocking: list[ExportBlocker]) -> None:
        self.reason: ExportBlockReason = reason
        self.blocking = list(blocking)
        super().__init__(
            f"export blocked ({reason}): {len(self.blocking)} undecided finding(s)"
            if reason == "undecided"
            else f"export blocked ({reason})"
        )


class NotChattable(ValueError):
    """The clause has no verdict in this review, so there is no finding to discuss
    (API: 409 `not_chattable`)."""


class DeletionBlocked(ValueError):
    """A review of the document is queued or running, so it cannot be deleted yet (API:
    409 `deletion_blocked`). Deleting under a live run would race its writes; wait for it
    to end (or fail it) and delete then. `review_ids` are the active reviews."""

    def __init__(self, review_ids: list[str]) -> None:
        self.review_ids = list(review_ids)
        super().__init__(f"a review is still running: {', '.join(self.review_ids)}")


@dataclass(frozen=True)
class ChatChunk:
    """One piece of a streamed model answer (Phase 8, 8.C).

    `text` is the next piece of the answer (may be empty). The last chunk carries the
    usage. A chunk with `error` ends the stream: the provider failed, which is data, not an
    exception, as in `core.llm`. Only `core.budget.BudgetExceeded` propagates.
    """

    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: str | None = None


# One generating call for the summary's recommendation paragraph: (system, user) → the
# completion, the shape of `core.llm.complete`; never raises on a provider error.
SummaryModel = Callable[[str, str], Completion]

# One streamed generating call for the rewrite chat: (system, user) → chunks, the last of
# which carries the usage (or an error).
ChatModel = Callable[[str, str], Iterator[ChatChunk]]


class ReviewService(Protocol):
    """Errors, which the API maps to responses: KeyError for an unknown review or
    document (404), ValueError for a request the current state forbids, e.g. retrying a
    clause that has not failed or reviewing a document with no clauses (409), and
    RuntimeError while shutting down (503)."""

    def start(self, document_id: str, scope: list[Topic]) -> Review:
        """Start a review, or return the active one for the same document and agent version."""
        ...

    def get(self, review_id: str) -> Review | None: ...

    def findings(self, review_id: str) -> list[Finding]:
        """Every clause of the document in order, with status, verdict, decision and vote."""
        ...

    def retry_clause(self, review_id: str, position: int) -> None:
        """Re-judge a failed clause. Raises ValueError if it is not failed."""
        ...

    def events(
        self, review_id: str, after_seq: int = 0, *, follow: bool = True
    ) -> Iterator[StoredEvent]:
        """Stored events with seq > after_seq, then live ones until RUN_FINISHED or RUN_ERROR.

        On a reconnect (after_seq > 0) a fresh STATE_SNAPSHOT is yielded first with seq=0:
        it is not stored, and the API sends it without an SSE `id` so it never moves the
        client's Last-Event-ID. The missed stored events follow, exactly once each.
        Blocks between live events; the API wraps it for SSE with keep-alive pings.
        """
        ...

    def explain_clause(self, review_id: str, position: int) -> Finding:
        """Regenerate a void verdict's missing reasoning (one model call); ValueError if not
        applicable."""
        ...

    def decide(
        self,
        review_id: str,
        position: int,
        decision: DecisionIn,
        reviewer: Reviewer,
        *,
        request_id: str | None = None,
    ) -> Finding:
        """Record a lawyer's decision and return the clause's finding as it now stands.

        In order: validate; if the clause is `awaiting_decision`, resume it (the live graph's
        checkpoint, or explain-only for a cached verdict) so a decided `potentially_void`
        gets its reasoning in `Decision.reason_de`; store the decision and its
        `DecisionEvent` atomically (`DecisionRepo.put`); score the feedback (accept 👍,
        override 👎 `verdict_wrong` plus `basis_wrong` if the § changed, dismiss a
        `dismissed` score); set the clause `done` if it was awaiting; recompute and store
        the counts; append the events docs/Events.md defines for a decision. Synchronous:
        the returned finding is final (never `checking`). Decisions on one clause are
        serialised, so a second one waits for the first's resume and is then an ordinary
        re-decision.

        KeyError: unknown review or position. NotDecidable: the clause has no verdict.
        InvalidDecision: override without `corrected_verdict`, `corrected_*` on accept or
        dismiss, an override identical to the model's verdict and basis, or an unknown
        chunk_id in `corrected_basis`. RuntimeError: shutting down. A model failure while
        resuming is not an error: the decision is kept with `reason_unavailable=True`.
        """
        ...

    def audit(self, review_id: str, position: int | None = None) -> list[DecisionEvent]:
        """The review's decision history (one position's, if given), oldest first.
        KeyError for an unknown review."""
        ...

    def agreement(self, agent_version: str | None = None) -> Agreement:
        """Lawyer agreement for `agent_version`, or for this service's agent version when
        None. A version without decisions gives zeros and `rate=None`."""
        ...

    def summary(self, review_id: str) -> ReviewSummary:
        """The review's summary block (Phase 8, 8.A), deterministic from its findings and
        current decisions (`app.domain.ReviewSummary` defines every field).

        With `summary.llm_enabled` the recommendation paragraph comes from one model call,
        cached by (`decision_set_hash`, model) in `CacheRepo.get_summary_text`, so asking
        again for the same decision set costs nothing. A model failure is not an error: the
        rule-based text is returned with `recommendation_from_llm=False` and nothing is
        cached. Works in every review status (a running review gives a draft summary).

        KeyError: unknown review.
        """
        ...

    def export(
        self,
        review_id: str,
        fmt: ExportFormat,
        *,
        draft: bool = False,
        reviewer: Reviewer,
    ) -> ExportFile:
        """Render the DD export (Phase 8, 8.B) in `fmt`: the memo as DOCX (from the template
        in `export.template_dir`) or Markdown, or the findings as CSV (docs/Phase 8 API.md
        has the layouts). Built only from the summary, the findings, the decisions and the
        statute texts; no model call (the summary's cached LLM paragraph is reused if there
        is one, never generated here). `reviewer` is who exports, named on the cover.

        Without `draft`: raises ExportBlocked while the review is not finished or
        awaiting_decisions, or while a void or review finding is undecided. With `draft`:
        never blocked; every format is marked `DRAFT_WATERMARK` and `ExportFile.draft` is
        True. The output is deterministic for a given review, decisions, reviewer and date
        (golden files), apart from the DOCX's own zip timestamps, which are fixed.

        KeyError: unknown review.
        """
        ...

    def chat(
        self,
        review_id: str,
        position: int,
        message: ChatIn,
        reviewer: Reviewer,
        *,
        request_id: str | None = None,
    ) -> Iterator[ChatEvent]:
        """Ask for a rewrite of one finding's clause (Phase 8, 8.C): one streamed model call.

        Validates when called, before anything is streamed: KeyError for an unknown review
        or position, NotChattable for a clause without a verdict, RuntimeError when no chat
        model is configured or the service is shutting down, `core.budget.BudgetExceeded`
        when the daily budget is spent. Then returns an iterator of the events
        docs/Events.md defines under "Rewrite chat": RUN_STARTED, TEXT_MESSAGE_START,
        TEXT_MESSAGE_CONTENT…, TEXT_MESSAGE_END, CUSTOM chat_result (`ChatResult`),
        RUN_FINISHED. A model failure after the start is a RUN_ERROR event, never an
        exception.

        The prompt's context is the clause (quoted as data between the explain step's
        `<klausel>` delimiters, defanged the same way), the cited statute texts, the
        decision and the reasoning; `message.history` is quoted as data too. The answer is
        capped at `chat.max_output_tokens` and `chat.max_answer_chars`; its suggestion is
        diffed word by word against the original clause. Nothing is stored and the review's
        cost is unchanged; the result carries its own tokens and cost.
        """
        ...

    def delete_document(
        self, document_id: str, principal: Principal, *, dry_run: bool = False
    ) -> DeletionReport:
        """Delete a document and everything only it used (Phase 9, 9.B; docs/Phase 9 API.md,
        "Deletion").

        With `dry_run`: the plan (`DeletionRepo.plan_document`), nothing changes. Otherwise
        `DeletionRepo.delete_document` in one transaction, then the queued erasures are
        attempted at once (`process_erasures`): blobs, Langfuse traces, scores and dataset
        items, checkpoints. One that fails stays pending for the nightly sweep and is
        reported without `done_at`; the request still succeeds, since the database part, the
        part the user sees, is gone.

        KeyError: unknown document (or, with login, one `principal` does not own: never
        reveal that it exists). DeletionBlocked: a review of it is queued or running.
        """
        ...

    def process_erasures(self, limit: int = 100) -> tuple[int, int]:
        """Attempt the pending erasure tasks, oldest first; returns (done, failed). Each is
        idempotent, so a retry after a crash is harmless."""
        ...

    def sweep(self, now: datetime | None = None) -> SweepReport:
        """The nightly job (9.B orphan sweep, 9.C retention): delete documents and reviews
        past `retention.files_days` / `reviews_days` through the same deletion, queue
        Langfuse traces past `traces_days`, remove orphans, then `process_erasures`. A no-op
        for every class whose period is null, and entirely while `retention.enabled` is
        false (the orphan sweep and the erasures still run)."""
        ...

    def resume_running(self) -> int:
        """Resume reviews left `running` by a restart; returns how many."""
        ...

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop runs after their in-flight clauses, leave them `running` for resume,
        and wake every blocked `events` iterator so SSE responses can end."""
        ...

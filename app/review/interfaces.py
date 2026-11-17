"""What the API asks of the review layer. The runner implements it; the API depends only on this."""

from collections.abc import Iterator
from typing import Protocol

from app.domain import (
    Agreement,
    DecisionEvent,
    DecisionIn,
    Finding,
    Review,
    Reviewer,
    StoredEvent,
    Topic,
)


class InvalidDecision(ValueError):
    """The decision itself is malformed (API: 422 `invalid_decision`), as opposed to a plain
    ValueError, which means the clause's state forbids it (409)."""


class NotDecidable(ValueError):
    """The clause has no verdict in this review (`queued`, `checking`, `failed`,
    `out_of_scope`), so there is nothing to decide (API: 409 `not_decidable`)."""


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

    def resume_running(self) -> int:
        """Resume reviews left `running` by a restart; returns how many."""
        ...

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop runs after their in-flight clauses, leave them `running` for resume,
        and wake every blocked `events` iterator so SSE responses can end."""
        ...

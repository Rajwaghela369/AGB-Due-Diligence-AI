"""What the API asks of the review layer. The runner implements it; the API depends only on this."""

from collections.abc import Iterator
from typing import Protocol

from app.domain import Finding, Review, StoredEvent, Topic


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

    def resume_running(self) -> int:
        """Resume reviews left `running` by a restart; returns how many."""
        ...

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop runs after their in-flight clauses, leave them `running` for resume,
        and wake every blocked `events` iterator so SSE responses can end."""
        ...

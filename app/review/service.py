"""The review service the API talks to (`app.review.interfaces.ReviewService`).

It owns the runs: each is a background thread, registered in `_runs` under one
lock that also covers the decisions that must not interleave — "is a review of
this document already active?" in `start`, "is this review's run still
taking work?" in `retry_clause`, and a run's own "nothing left, deregister".
That lock is never held while waiting for a run, only while deciding, so a
run finishing can always take it.

`shutdown()` stops runs without finishing them: their reviews stay `running`,
which is exactly what `resume_running()` looks for on the next start, and the
verdict cache makes the resumed run pay only for clauses never judged.
"""

import threading
import uuid
from collections.abc import Iterator
from typing import Any

from ag_ui.core import RunErrorEvent, StateSnapshotEvent
from core.logging import get_logger

from app.domain import Finding, Review, StoredEvent, Topic
from app.review.events import EventBus, serialise
from app.review.limiter import DEFAULT_MAX_CONCURRENCY
from app.review.runner import ReviewRun, RunContext, build_state, load_entries
from app.store.interfaces import Store

log = get_logger("review.service")


class Service:
    """Implements `ReviewService`. Build it with `build_service`."""

    def __init__(
        self,
        store: Store,
        judge: Any,
        limiter: Any,
        tracer: Any = None,
        *,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        statutes: Any = None,
        prices: dict[str, float] | None = None,
        poll_seconds: float = 0.5,
        recheck_seconds: float = 5.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.store = store
        self.judge = judge
        self.limiter = limiter
        self.statutes = statutes  # kept for the API; the runner itself does not read statutes
        self.bus = EventBus(store, recheck_seconds=recheck_seconds)
        self._ctx = RunContext(
            store=store,
            judge=judge,
            limiter=limiter,
            bus=self.bus,
            tracer=tracer,
            max_concurrency=max_concurrency,
            prices=dict(prices or {}),
            poll_seconds=poll_seconds,
        )
        self._lock = threading.RLock()
        self._runs: dict[str, ReviewRun] = {}
        self._closed = False

    @property
    def agent_version(self) -> str:
        return self.judge.agent_version

    # --- ReviewService ----------------------------------------------------------

    def start(self, document_id: str, scope: list[Topic]) -> Review:
        """Start a review, or return the active one for this document and agent version.

        Raises KeyError for an unknown document and ValueError for one without clauses.
        """
        with self._lock:
            self._check_open()
            active = self.store.reviews.active_for(document_id, self.agent_version)
            if active is not None:
                return active
            if self.store.documents.get(document_id) is None:
                raise KeyError(f"unknown document {document_id}")
            clauses = self.store.documents.clauses(document_id)
            if not clauses:
                raise ValueError(f"document {document_id} has no clauses")
            review = self.store.reviews.create(
                Review(
                    review_id=uuid.uuid4().hex,
                    document_id=document_id,
                    agent_version=self.agent_version,
                    scope=list(scope),
                    status="queued",
                )
            )
            self._launch(review, clauses, positions=None, reason="start")
            return self.store.reviews.get(review.review_id)  # type: ignore[return-value]

    def get(self, review_id: str) -> Review | None:
        return self.store.reviews.get(review_id)

    def findings(self, review_id: str) -> list[Finding]:
        """Every clause in document order. Raises KeyError for an unknown review."""
        review = self._review(review_id)
        clauses = self.store.documents.clauses(review.document_id)
        entries = load_entries(self.store, review, clauses)
        decisions = self.store.decisions.for_review(review_id)
        votes = self.store.feedback.for_review(review_id)
        findings = []
        for clause in clauses:
            status, error, verdict = entries[clause.position]
            findings.append(
                Finding(
                    position=clause.position,
                    clause=clause,
                    status=status,
                    verdict=verdict,
                    error=error,
                    decision=decisions.get(clause.position),
                    feedback=votes.get(clause.position),
                )
            )
        return findings

    def retry_clause(self, review_id: str, position: int) -> None:
        """Re-judge a failed clause. KeyError for an unknown review, ValueError if not failed."""
        with self._lock:
            self._check_open()
            review = self._review(review_id)
            run = self._runs.get(review_id)
            if run is not None:
                run.add(position)  # still running: join its queue
                return
            status = self.store.reviews.clause_statuses(review_id).get(position, ("queued",))[0]
            if status != "failed":
                raise ValueError(f"clause {position} is {status}, not failed")
            if review.agent_version != self.agent_version:
                raise ValueError("the agent has changed since this review; start a new review")
            clauses = self.store.documents.clauses(review.document_id)
            self._launch(review, clauses, positions=[position], reason="retry")

    def events(
        self, review_id: str, after_seq: int = 0, *, follow: bool = True
    ) -> Iterator[StoredEvent]:
        """See `ReviewService.events`. Raises KeyError at once for an unknown review."""
        self._review(review_id)
        return self._events(review_id, after_seq, follow)

    def _events(self, review_id: str, after_seq: int, follow: bool) -> Iterator[StoredEvent]:
        if after_seq > 0:
            # Built from the store when the client reconnects. Anything it already reflects
            # and is replayed below is a `replace`, so applying it twice changes nothing.
            review = self._review(review_id)
            clauses = self.store.documents.clauses(review.document_id)
            state = build_state(review, load_entries(self.store, review, clauses))
            yield StoredEvent(
                review_id=review_id,
                seq=0,
                type="STATE_SNAPSHOT",
                payload=serialise(StateSnapshotEvent(snapshot=state)),
            )
        yield from self.bus.subscribe(review_id, after_seq, follow=follow)

    def resume_running(self) -> int:
        """Resume reviews left `running` by a restart; returns how many were resumed.

        A review judged by another agent version cannot be resumed honestly (its
        verdicts would mix versions), so it is failed with RUN_ERROR instead.
        """
        resumed = 0
        with self._lock:
            self._check_open()
            for review in self.store.reviews.running():
                if review.review_id in self._runs:
                    continue
                if review.agent_version != self.agent_version:
                    message = (
                        f"agent version changed from {review.agent_version} to "
                        f"{self.agent_version}; start a new review"
                    )
                    self.store.reviews.set_status(review.review_id, "failed", message)
                    self.bus.emit(
                        review.review_id,
                        RunErrorEvent(message=message, code="agent_version_changed"),
                    )
                    continue
                clauses = self.store.documents.clauses(review.document_id)
                self._launch(review, clauses, positions=None, reason="resume")
                resumed += 1
        return resumed

    # --- lifecycle --------------------------------------------------------------

    def wait(self, review_id: str, timeout: float | None = None) -> bool:
        """Block until this service has no run of the review; False on timeout."""
        with self._lock:
            run = self._runs.get(review_id)
        return run is None or run.join(timeout)

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop every run after its in-flight clauses, then end every event stream.

        Reviews stay `running` so the next process resumes them. In-flight judge
        calls cannot be interrupted; `timeout` bounds the wait for each run.
        """
        with self._lock:
            self._closed = True
            runs = list(self._runs.values())
            for run in runs:
                run.stop()
        for run in runs:  # not under the lock: a stopping run takes it to deregister
            if not run.join(timeout):
                log.warning("review %s did not stop within %ss", run.review_id, timeout)
        self.bus.shutdown()

    # --- internals ----------------------------------------------------------------

    def _launch(self, review: Review, clauses, *, positions: list[int] | None, reason: str) -> None:
        """Open a run, mark the review running and start its thread. Caller holds the lock."""
        run = ReviewRun(
            self._ctx,
            review,
            clauses,
            positions=positions,
            reason=reason,
            finish_gate=self._lock,
            on_closed=self._deregister,
        )
        try:
            run.open()
            self.store.reviews.set_status(review.review_id, "running")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.store.reviews.set_status(review.review_id, "failed", message)
            raise
        self._runs[review.review_id] = run
        run.start()

    def _deregister(self, run: ReviewRun) -> None:
        with self._lock:
            if self._runs.get(run.review_id) is run:
                del self._runs[run.review_id]

    def _review(self, review_id: str) -> Review:
        review = self.store.reviews.get(review_id)
        if review is None:
            raise KeyError(f"unknown review {review_id}")
        return review

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the review service is shutting down")


def build_service(
    store: Store,
    judge: Any,
    limiter: Any,
    tracer: Any = None,
    *,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    statutes: Any = None,
    prices: dict[str, float] | None = None,
    poll_seconds: float = 0.5,
    recheck_seconds: float = 5.0,
) -> Service:
    """The one review service of a process; see `app.review.limiter.review_settings`."""
    return Service(
        store,
        judge,
        limiter,
        tracer,
        max_concurrency=max_concurrency,
        statutes=statutes,
        prices=prices,
        poll_seconds=poll_seconds,
        recheck_seconds=recheck_seconds,
    )

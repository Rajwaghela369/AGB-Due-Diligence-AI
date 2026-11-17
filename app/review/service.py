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
import time
import uuid
from typing import Any

from ag_ui.core import RunErrorEvent, RunFinishedEvent, StateSnapshotEvent
from core.logging import get_logger

from app.domain import (
    IDLE_REVIEW_STATUSES,
    Agreement,
    ClauseSpan,
    ClauseVerdict,
    Decision,
    DecisionEvent,
    DecisionIn,
    Finding,
    Review,
    Reviewer,
    StoredEvent,
    Topic,
)
from app.review.events import TERMINAL_TYPES, EventBus, Subscription, serialise
from app.review.limiter import DEFAULT_MAX_CONCURRENCY
from app.review.runner import (
    ReviewRun,
    RunContext,
    build_state,
    load_entries,
    regenerate_reason,
    review_clauses,
)
from app.review.topics import Router, tag_topics
from app.store.interfaces import Store

DEFAULT_ROUTER_MODEL = "jina-embeddings-v3"
WAVE_1 = "Phase 7 Wave 1"

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
        router: Router | None = None,
        router_model: str = DEFAULT_ROUTER_MODEL,
        feedback: Any = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.store = store
        self.judge = judge
        self.limiter = limiter
        self.statutes = statutes  # kept for the API; the runner itself does not read statutes
        self.router = router  # None: clauses keep no topic and every in-scope clause is judged
        self.router_model = router_model
        self.feedback = feedback  # app.review.feedback.FeedbackService, used by `decide`
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
        self._stopped: dict[str, ReviewRun] = {}  # stopped by shutdown, for wait()
        self._closed = False

    @property
    def agent_version(self) -> str:
        return self.judge.agent_version

    # --- ReviewService ----------------------------------------------------------

    def start(self, document_id: str, scope: list[Topic]) -> Review:
        """Start a review, or return the active one for this document and agent version.

        Raises KeyError for an unknown document and ValueError for one without clauses.
        The clauses are tagged with topics first, outside the lock (it may call the router),
        and the active-review check is repeated after it.
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
        topics = self._tag(clauses)
        with self._lock:
            self._check_open()
            active = self.store.reviews.active_for(document_id, self.agent_version)
            if active is not None:
                return active
            review_id = uuid.uuid4().hex
            review = self.store.reviews.create(
                Review(
                    review_id=review_id,
                    document_id=document_id,
                    agent_version=self.agent_version,
                    scope=list(scope),
                    status="queued",
                )
            )
            if review.review_id != review_id:
                return review  # another process won the race (reviews_one_active_idx)
            try:
                self.store.reviews.set_clause_topics(review_id, topics)
            except Exception as exc:
                self.store.reviews.set_status(review_id, "failed", f"{type(exc).__name__}: {exc}")
                raise
            for clause in clauses:
                clause.topic = topics.get(clause.position, clause.topic)
            self._launch(review, clauses, positions=None, reason="start")
            return self.store.reviews.get(review.review_id)  # type: ignore[return-value]

    def get(self, review_id: str) -> Review | None:
        return self.store.reviews.get(review_id)

    def findings(self, review_id: str) -> list[Finding]:
        """Every clause in document order. Raises KeyError for an unknown review."""
        review = self._review(review_id)
        clauses = review_clauses(self.store, review)
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
            active = self.store.reviews.active_for(review.document_id, review.agent_version)
            if active is not None and active.review_id != review_id:
                raise ValueError(f"review {active.review_id} of this document is running")
            clauses = review_clauses(self.store, review)
            self._launch(review, clauses, positions=[position], reason="retry")

    def explain_clause(self, review_id: str, position: int) -> Finding:
        """Regenerate the missing reasoning of a void verdict (1 Gemini call).

        KeyError for an unknown review or clause; ValueError (409) if the verdict is not
        void or already has a reason; `ExplainFailed` if the provider failed.
        """
        with self._lock:
            self._check_open()
            review = self._review(review_id)
        explain = getattr(self.judge, "explain_only", None)
        if explain is None:
            raise RuntimeError("this agent cannot regenerate a reasoning")
        clause = next(
            (c for c in review_clauses(self.store, review) if c.position == position),
            None,
        )
        if clause is None:
            raise KeyError(f"unknown clause {position}")
        if self.store.reviews.clause_statuses(review_id).get(position, ("queued",))[0] != "done":
            raise ValueError(f"clause {position} has no verdict yet")
        verdict = self.store.verdicts.get(clause.content_hash, review.agent_version)
        if verdict is None:
            raise ValueError(f"clause {position} has no verdict yet")
        regenerate_reason(
            self._ctx,
            review_id,
            clause,
            verdict,
            gate=self._lock,
            active_runs=lambda: list(self._runs.values()),
        )
        return next(f for f in self.findings(review_id) if f.position == position)

    def events(self, review_id: str, after_seq: int = 0, *, follow: bool = True) -> Subscription:
        """See `ReviewService.events`. Raises KeyError at once for an unknown review.

        The iterator's `close()` is thread-safe and ends a blocked `next()`.
        """
        self._reconcile(self._review(review_id))
        return self._events(review_id, after_seq, follow)

    def _reconcile(self, review: Review) -> None:
        """Append the terminal event a finished or failed review is missing.

        Status and terminal event are two writes; a crash (or a failed append)
        between them would leave followers waiting for an event that never
        comes. Done when the review is read, which covers every review a client
        can ask for, rather than by scanning the table at startup.
        """
        if review.status not in IDLE_REVIEW_STATUSES:
            return
        with self._lock:
            if review.review_id in self._runs:
                return
            events = list(self.store.events.since(review.review_id, 0))
            if events and events[-1].type in TERMINAL_TYPES:
                return
            if review.status != "failed":
                result = {
                    "counts": review.counts.model_dump(mode="json"),
                    "cost_usd": review.cost_usd,
                }
                event = RunFinishedEvent(
                    thread_id=review.document_id, run_id=review.review_id, result=result
                )
            else:
                event = RunErrorEvent(
                    message=review.error or "the review failed", code="review_failed"
                )
            log.warning(
                "review %s was %s without a terminal event", review.review_id, review.status
            )
            self.bus.emit(review.review_id, event)

    def _events(self, review_id: str, after_seq: int, follow: bool) -> Subscription:
        first = []
        if after_seq > 0:
            # Built from the store when the client reconnects. Anything it already reflects
            # and is replayed below is a `replace`, so applying it twice changes nothing.
            review = self._review(review_id)
            clauses = review_clauses(self.store, review)
            state = build_state(review, load_entries(self.store, review, clauses))
            first.append(
                StoredEvent(
                    review_id=review_id,
                    seq=0,
                    type="STATE_SNAPSHOT",
                    payload=serialise(StateSnapshotEvent(snapshot=state)),
                )
            )
        return self.bus.subscribe(review_id, after_seq, follow=follow, first=first)

    # --- Phase 7: decisions (Wave 1 implements these; signatures are the contract) -----

    def decide(
        self,
        review_id: str,
        position: int,
        decision: DecisionIn,
        reviewer: Reviewer,
        *,
        request_id: str | None = None,
    ) -> Finding:
        """See `ReviewService.decide`. Owner: decisions-api. For an `awaiting_decision`
        clause it calls `_resume_decided` (owner: interrupt) before storing the decision."""
        raise NotImplementedError(WAVE_1)

    def audit(self, review_id: str, position: int | None = None) -> list[DecisionEvent]:
        """See `ReviewService.audit`. Owner: decisions-api."""
        raise NotImplementedError(WAVE_1)

    def agreement(self, agent_version: str | None = None) -> Agreement:
        """See `ReviewService.agreement`. Owner: decisions-api."""
        raise NotImplementedError(WAVE_1)

    def _resume_decided(
        self, review: Review, clause: ClauseSpan, verdict: ClauseVerdict, decision: Decision
    ) -> tuple[Decision, float]:
        """Resume an `awaiting_decision` clause with the lawyer's decision. Owner: interrupt.

        Called by `decide` for every action on an awaiting clause, after validation and
        before `DecisionRepo.put`, outside the service lock. `decision` is complete except
        for the reasoning. With a live checkpoint (thread `{review_id}:{position}`) the graph
        resumes with the decided verdict and basis; without one (a cached verdict) the judge
        explains only. Either way the explanation runs only when `decided_verdict(decision)`
        is potentially_void. Returns the decision with `reason_de`/`reason_unavailable` set
        when it ran, and the USD cost of the model calls made (added to `cost_usd`). Never
        raises for a model failure: `reason_unavailable=True`. Emits no events and does not
        change the clause status; `decide` does both.

        Until Wave 1 this makes no model call.
        """
        return decision, 0.0

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
                clauses = review_clauses(self.store, review)
                self._launch(review, clauses, positions=None, reason="resume")
                resumed += 1
        return resumed

    # --- lifecycle --------------------------------------------------------------

    def wait(self, review_id: str, timeout: float | None = None) -> bool:
        """Block until this service has no run of the review (nor a clause of a stopped
        one still in flight); False on timeout."""
        with self._lock:
            run = self._runs.get(review_id) or self._stopped.get(review_id)
        if run is None:
            return True
        deadline = None if timeout is None else time.monotonic() + timeout
        if not run.join(timeout):
            return False
        return run.drain(None if deadline is None else max(0.0, deadline - time.monotonic()))

    def shutdown(self, timeout: float | None = None) -> None:
        """Stop every run, interrupt limiter waits, then end every event stream.

        Reviews stay `running` so the next process resumes them. Closing the
        limiter wakes clauses waiting on it (even until tomorrow) with
        `LimiterClosed`; they stay `checking` and resume. A judge call already
        talking to the provider runs to its own timeout. `timeout` bounds the
        whole wait; whatever is still in flight then is left to finish alone.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def left() -> float | None:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        with self._lock:
            self._closed = True
            runs = list(self._runs.values())
            for run in runs:
                run.stop()
                self._stopped[run.review_id] = run
        close = getattr(self.limiter, "close", None)
        if close is not None:
            try:
                close()
            except Exception:  # noqa: BLE001 - shutting down regardless
                log.warning("could not close the limiter", exc_info=True)
        for run in runs:  # not under the lock: a stopping run takes it to deregister
            if not (run.join(left()) and run.drain(left())):
                log.warning("review %s still had clauses in flight at shutdown", run.review_id)
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
            run.announce()  # in the log before anyone can see the review running
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

    def _tag(self, clauses: list[ClauseSpan]) -> dict[int, str]:
        if self.router is None:
            return {}
        return tag_topics(
            clauses, router=self.router, model=self.router_model, caches=self.store.caches
        )

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
    router: Router | None = None,
    router_model: str = DEFAULT_ROUTER_MODEL,
    feedback: Any = None,
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
        router=router,
        router_model=router_model,
        feedback=feedback,
    )

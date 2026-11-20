"""Run one review with cache-first judging, concurrency, and event emission.

A run is a start, a resume after a restart, or a retry of failed clauses; all
three are the same loop over a different set of positions. The run owns a
small thread pool and never has more than `max_concurrency` clauses in it, so
the pool's queue is always empty. A stop (shutdown) does not wait for clauses
in flight: the service closes the shared limiter, which wakes any clause
waiting on it with `LimiterClosed`, and such an interrupted clause is left
`checking` (not failed) so the next process resumes it.

The run announces itself before the review is marked running, so every visible running
review has a corresponding start event.

Paying once. Clauses are judged under a per-(content_hash, agent_version) lock
shared by every run in the process, with a cache re-check after taking it: a
clause repeated within a document, or shared by two concurrent reviews, costs
one judge call; the others are cache hits. The lock is in-process only, which
is enough for v1's single API worker; several workers would need a Postgres
advisory lock on the same key.

Cost after a crash. `cost_usd` is the sum of what this review's own judge calls
cost, kept in memory and persisted with the counts. If the process dies after
a verdict is saved but before its cost is persisted, the resumed run finds the
verdict in the cache and counts a free cache hit, so `cost_usd` undercounts by
that clause. Accepted for v1; exact accounting needs a per-review charge
written in the same transaction as the verdict.

Ordering. Every change to a clause is made, persisted and emitted while the
run's lock is held, so the event log is a faithful, ordered replay of the
state: deltas apply in `seq` order, and each `progress` carries counts at
least as new as the delta before it. The store is written before the event, so
a snapshot built from the store is never behind an event a client has seen.

A clause failure is the clause's (Phase 5 principle 3): any exception from the
judge or the verdict write marks that clause `failed` and the run carries on.
Only an error outside a clause (the store failing at the start, say) ends the
run with RUN_ERROR.

Scope. A clause is out of scope when ingest marked it so, or when it carries a
topic that is not in the review's scope. Topics are assigned per review when it
starts (`app.review.topics`) and stored on its clause rows; `review_clauses`
puts them back on the clauses, so every path that loads a review's clauses
scopes them the same way. A clause without a topic (no router, or the router
failed) is judged.

Waiting for the lawyer (Phase 7). A clause whose verdict is `uncertain` and that has no
decision yet becomes `awaiting_decision` instead of `done`, whether the verdict was just
judged or came from the cache; its `CUSTOM interrupt` follows the delta that says so. A
live judge with a checkpointer has also stopped its graph under thread
`{review_id}:{position}` (`thread_id`), so a decision can resume it; a cached verdict has
no graph, and a decision explains only (`Service._resume_decided`). The worker is free
at once either way. A run ends `awaiting_decisions` while any clause still waits.
"""

import threading
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import (
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from core.budget import LimiterClosed
from core.logging import bind, get_logger, update

from app.agent.run import ClauseFailed
from app.domain import (
    CUSTOM_INTERRUPT,
    CUSTOM_PROGRESS,
    IDLE_REVIEW_STATUSES,
    ClauseSpan,
    ClauseStatus,
    ClauseVerdict,
    Decision,
    Interrupt,
    Review,
    ReviewCounts,
)
from app.review import avv
from app.review.events import EventBus
from app.review.tracing import ReviewTrace, start_review_trace
from app.store.interfaces import Store

log = get_logger("review.runner")


class KeyedLocks:
    """One lock per key, created on demand and dropped when nobody holds or waits for it."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[Any, list] = {}  # key -> [lock, holders and waiters]

    @contextmanager
    def hold(self, key: Any) -> Iterator[None]:
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)


def interrupted(exc: BaseException) -> bool:
    """True if a judge error was caused by the limiter closing (shutdown), however wrapped."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, LimiterClosed):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


@dataclass
class RunContext:
    """What every run shares: the service's dependencies and settings."""

    store: Store
    judge: Any  # app.agent.run.Judge
    limiter: Any  # core.budget.SharedLimiter
    bus: EventBus
    tracer: Any = None
    max_concurrency: int = 4
    prices: dict[str, float] = field(default_factory=dict)  # USD per million tokens
    poll_seconds: float = 0.5  # how often a run looks at the limiter's waiting_reason
    flights: KeyedLocks = field(default_factory=KeyedLocks)  # single-flight judge calls
    verdict_cache: dict[tuple[str, str], ClauseVerdict] = field(default_factory=dict)
    verdict_cache_lock: threading.Lock = field(default_factory=threading.Lock)


# --- state ------------------------------------------------------------------


# Statuses in which a clause's stored verdict is shown (and, for the first two, counted).
VERDICT_STATUSES: tuple[ClauseStatus, ...] = ("done", "awaiting_decision", "checking")


def review_clauses(store: Store, review: Review) -> list[ClauseSpan]:
    """The document's clauses with the topics this review tagged them with."""
    clauses = store.documents.clauses(review.document_id)
    topics = store.reviews.clause_topics(review.review_id)
    for clause in clauses:
        if clause.position in topics:
            clause.topic = topics[clause.position]
    return clauses


def in_scope(clause: ClauseSpan, review: Review) -> bool:
    if not clause.in_scope:
        return False
    if review.document_type == "avv":  # topics are AVV routes there (app.review.avv)
        return clause.topic != avv.UNROUTED
    return clause.topic is None or clause.topic in review.scope


def clause_entry(
    status: ClauseStatus, error: str | None = None, verdict: ClauseVerdict | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "verdict": verdict.model_dump(mode="json") if verdict is not None else None,
    }


def load_entries(
    store: Store, review: Review, clauses: list[ClauseSpan]
) -> dict[int, tuple[ClauseStatus, str | None, ClauseVerdict | None]]:
    """Each clause's status, error and verdict as the store has them right now."""
    statuses = store.reviews.clause_statuses(review.review_id)
    judged_hashes = [
        clause.content_hash
        for clause in clauses
        if statuses.get(clause.position, ("queued", None))[0] in VERDICT_STATUSES
    ]
    verdicts = store.verdicts.get_many(judged_hashes, review.agent_version)
    entries: dict[int, tuple[ClauseStatus, str | None, ClauseVerdict | None]] = {}
    for clause in clauses:
        if not in_scope(clause, review):
            entries[clause.position] = ("out_of_scope", None, None)
            continue
        status, error = statuses.get(clause.position, ("queued", None))
        verdict = verdicts.get(clause.content_hash) if status in VERDICT_STATUSES else None
        entries[clause.position] = (status, error, verdict)
    return entries


def thread_id(review_id: str, position: int) -> str:
    """The checkpoint thread of one clause of one review (Phase 7 plan, 7.B)."""
    return f"{review_id}:{position}"


def settled_status(verdict: ClauseVerdict, decided: bool) -> ClauseStatus:
    """The status a clause takes when its verdict arrives: an undecided `uncertain` waits."""
    return "awaiting_decision" if verdict.verdict == "uncertain" and not decided else "done"


def interrupt_event(position: int, verdict: ClauseVerdict) -> CustomEvent:
    value = Interrupt(position=position, verdict=verdict.verdict, confidence=verdict.confidence)
    return CustomEvent(name=CUSTOM_INTERRUPT, value=value.model_dump(mode="json"))


def price(prices: dict[str, float], input_tokens: int, output_tokens: int) -> float:
    """USD for a number of tokens at `prices` (USD per million tokens)."""
    per_input = prices.get("input", 0.0)
    per_output = prices.get("output", 0.0)
    return (input_tokens * per_input + output_tokens * per_output) / 1e6


def build_state(
    review: Review, entries: dict[int, tuple], missing: list | None = None
) -> dict[str, Any]:
    """The state document of docs/Events.md. `missing`: the AVV document-level findings
    (`app.review.avv.review_missing`), always empty for AGB."""
    return {
        "review": review.model_dump(mode="json"),
        "missing_obligations": [item.model_dump(mode="json") for item in missing or []],
        "clauses": {
            str(pos): clause_entry(status, error, verdict)
            for pos, (status, error, verdict) in sorted(entries.items())
        },
    }


def count(
    entries: dict[int, tuple],
    cache_hits: int,
    decisions: dict[int, Decision] | None = None,
) -> ReviewCounts:
    """The review's `ReviewCounts` (see its docstring for what each field means).

    The one place counts are computed: every writer of `reviews.counts` (a run, a decision)
    passes the current decisions, so no writer can reset another's tallies.
    """
    decisions = decisions or {}
    counts = ReviewCounts(cache_hits=cache_hits)
    for position, (status, _error, verdict) in entries.items():
        if status == "out_of_scope":
            counts.out_of_scope += 1
            continue
        counts.total += 1
        if status == "failed":
            counts.failed += 1
            continue
        if status == "done":
            counts.done += 1
        elif status == "awaiting_decision":
            counts.awaiting_decision += 1
        else:
            continue  # queued or checking: no verdict counted yet
        if verdict is None:
            continue
        setattr(counts, verdict.verdict, getattr(counts, verdict.verdict) + 1)
        if position in decisions:
            counts.decided += 1
        elif verdict.verdict == "potentially_void":
            counts.open_void += 1
        elif verdict.verdict == "uncertain":
            counts.open_review += 1
    return counts


def regenerate_reason(
    ctx: RunContext,
    review_id: str,
    clause: ClauseSpan,
    verdict: ClauseVerdict,
    *,
    gate: threading.RLock,
    active_runs: Callable[[], list["ReviewRun"]],
) -> ClauseVerdict:
    """Re-run the explanation of one void verdict and store it on the shared verdict row.

    The paid call runs under the verdict's single-flight lock only, so two clicks cost
    one Gemini call: the second finds the reason in place and gets ValueError (409).
    Storing and announcing happen under `gate` (the service lock, the one `_try_finish`
    holds), and whether the review is final is decided there, after the call returned:

    - a run is still going: only a STATE_DELTA, so the run's own terminal comes after it;
    - the review is final: the sequence docs/Events.md defines for a retry on a finished
      review, RUN_STARTED, STATE_SNAPSHOT, the clause's STATE_DELTA, then the terminal
      event again. No terminal is ever followed by anything but a RUN_STARTED.

    Runs still holding the old verdict in memory are refreshed so they cannot emit it.
    """
    key = (verdict.content_hash, verdict.agent_version)
    with ctx.flights.hold(key):
        current = ctx.store.verdicts.get(*key)
        if current is None or current.verdict != "potentially_void":
            raise ValueError("Nur bei als unwirksam eingestuften Klauseln möglich.")
        if not current.reason_unavailable:
            raise ValueError("Für diese Klausel liegt bereits eine Begründung vor.")
        explained = ctx.judge.explain_only(clause, current)  # raises ExplainFailed
        with gate:
            stored = ctx.store.verdicts.set_reason(
                key[0],
                key[1],
                explained.reason_de,
                explained.input_tokens - current.input_tokens,
                explained.output_tokens - current.output_tokens,
            )
            if stored is None:
                raise ValueError("Für diese Klausel liegt bereits eine Begründung vor.")
            with ctx.verdict_cache_lock:
                ctx.verdict_cache[key] = stored
            runs = active_runs()
            for run in runs:  # any review may hold the shared verdict
                run.refresh_verdict(stored)
            runs = [run for run in runs if run.review_id == review_id]
            review = ctx.store.reviews.get(review_id)
            if review is None:
                raise KeyError(f"unknown review {review_id}")
            statuses = ctx.store.reviews.clause_statuses(review_id)
            status, error = statuses.get(clause.position, ("done", None))
            delta = StateDeltaEvent(
                delta=[
                    {
                        "op": "replace",
                        "path": f"/clauses/{clause.position}",
                        "value": clause_entry(status, error, stored),
                    }
                ]
            )
            events: list = [delta]
            if not runs and review.status in IDLE_REVIEW_STATUSES:
                clauses = review_clauses(ctx.store, review)
                entries = load_entries(ctx.store, review, clauses)
                missing = avv.review_missing(
                    ctx.store, review, getattr(ctx.judge, "profile", None), entries
                )
                state = build_state(review, entries, missing)
                if review.status != "failed":
                    terminal: Any = RunFinishedEvent(
                        thread_id=review.document_id,
                        run_id=review_id,
                        result={
                            "counts": review.counts.model_dump(mode="json"),
                            "cost_usd": review.cost_usd,
                        },
                    )
                else:
                    terminal = RunErrorEvent(
                        message=review.error or "the review failed", code="review_failed"
                    )
                events = [
                    RunStartedEvent(thread_id=review.document_id, run_id=review_id),
                    StateSnapshotEvent(snapshot=state),
                    delta,
                    terminal,
                ]
            ctx.bus.emit_many(review_id, events)
        return stored


# --- the run ------------------------------------------------------------------


class ReviewRun:
    """Judges `positions` of one review on a background thread.

    Lifecycle, under the service's lock: `open()` loads and persists the
    starting state, `announce()` appends RUN_STARTED and STATE_SNAPSHOT, the
    service marks the review running, and `start()` launches the thread. The
    thread opens the trace, works the queue, and finishes under `finish_gate`
    (the service's lock) so that "nothing left to do" and "deregistered" are
    one step as far as a concurrent `retry_clause` can tell.
    """

    def __init__(
        self,
        ctx: RunContext,
        review: Review,
        clauses: list[ClauseSpan],
        *,
        positions: list[int] | None,
        reason: str,
        finish_gate: threading.RLock,
        on_closed: Callable[["ReviewRun"], None],
    ) -> None:
        self.ctx = ctx
        self.review = review.model_copy(deep=True)
        self.review_id = review.review_id
        self.reason = reason
        self._clauses = {c.position: c for c in clauses}
        self._requested = positions
        self._finish_gate = finish_gate
        self._on_closed = on_closed

        self._cond = threading.Condition()
        self._entries: dict[int, tuple[ClauseStatus, str | None, ClauseVerdict | None]] = {}
        self._cached_verdicts: dict[str, ClauseVerdict] = {}
        self._decisions: dict[int, Decision] = {}  # current decisions, for the counts
        self._pending: deque[int] = deque()
        self._cached_batch: list[int] = []  # known cache hits, settled before any judging
        self._inflight = 0
        self._cache_hits = review.counts.cache_hits
        self._cost = review.cost_usd
        self._announced = False  # RUN_STARTED and STATE_SNAPSHOT are in the log
        self._closed = False  # finished or stopped; accepts no more work
        self._stopping = False
        self._waiting_reason: str | None = None
        self._trace = ReviewTrace()
        self._thread = threading.Thread(
            target=self._run, name=f"review-{self.review_id[:8]}", daemon=True
        )

    # --- called by the service --------------------------------------------

    def open(self) -> None:
        """Load the review's clause state and queue the positions this run judges."""
        store = self.ctx.store
        entries = load_entries(store, self.review, list(self._clauses.values()))
        decisions = store.decisions.for_review(self.review_id)
        repaired = {
            position
            for position, (status, _, _) in entries.items()
            if status == "awaiting_decision" and position in decisions
        }
        for position in repaired:
            _status, error, verdict = entries[position]
            entries[position] = ("done", error, verdict)
        if self._requested is None:  # start or resume: everything not yet judged
            todo = [p for p, (s, _, _) in sorted(entries.items()) if s in ("queued", "checking")]
        else:
            todo = sorted(self._requested)
        try:
            self._cached_verdicts = store.verdicts.get_many(
                [self._clauses[position].content_hash for position in todo],
                self.review.agent_version,
            )
            with self.ctx.verdict_cache_lock:
                self.ctx.verdict_cache.update(
                    {
                        (content_hash, self.review.agent_version): verdict
                        for content_hash, verdict in self._cached_verdicts.items()
                    }
                )
        except Exception:  # noqa: BLE001 - a cache read failing only costs judge calls
            log.warning("bulk verdict cache read failed for %s", self.review_id, exc_info=True)
            self._cached_verdicts = {}
        with self._cond:
            self._entries = entries
            self._decisions = decisions
            statuses: dict[int, tuple[ClauseStatus, str | None]] = {
                pos: ("out_of_scope", None)
                for pos, (status, _, _) in entries.items()
                if status == "out_of_scope"
            }
            statuses.update({position: ("done", None) for position in repaired})
            for pos in todo:
                self._entries[pos] = ("queued", None, None)
                statuses[pos] = ("queued", None)
                if self._clauses[pos].content_hash in self._cached_verdicts:
                    self._cached_batch.append(pos)  # resolved in one go by `_resolve_cached`
                else:
                    self._pending.append(pos)
            store.reviews.set_clause_statuses(self.review_id, statuses)
            self._save_counts()

    def start(self) -> None:
        self._thread.start()

    def add(self, position: int) -> None:
        """Queue a failed clause again (retry while the run is still going)."""
        with self._cond:
            if self._closed or self._stopping:
                raise RuntimeError("the run is stopping")
            status = self._entries.get(position, (None,))[0]
            if status != "failed":
                raise ValueError(f"clause {position} is {status or 'unknown'}, not failed")
            self._pending.append(position)
            self._set(position, "queued")
            self._cond.notify_all()

    def refresh_verdict(self, verdict: ClauseVerdict) -> None:
        """Swap in a verdict stored after this run read it, so the run cannot emit the old one."""
        with self._cond:
            if verdict.content_hash in self._cached_verdicts:
                self._cached_verdicts[verdict.content_hash] = verdict
            for position, (status, error, held) in self._entries.items():
                if held is not None and held.content_hash == verdict.content_hash:
                    self._entries[position] = (status, error, verdict)

    def note_decision(self, decision: Decision, cost_usd: float = 0.0) -> ReviewCounts:
        """Take a decision stored while this run is going into its counts and cost; persist
        and return them. A waiting clause becomes `done` here too, so the run neither ends
        `awaiting_decisions` for it nor overwrites the stored counts with stale ones. The
        caller emits the events (docs/Events.md, decisions)."""
        with self._cond:
            self._decisions[decision.position] = decision.model_copy(deep=True)
            status, error, verdict = self._entries.get(decision.position, (None, None, None))
            if status == "awaiting_decision":
                self._entries[decision.position] = ("done", error, verdict)
            self._cost += cost_usd
            return self._save_counts()

    def stop(self) -> None:
        """Stop taking clauses; in-flight ones finish. The review stays `running` for resume."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()

    def join(self, timeout: float | None = None) -> bool:
        if self._thread.ident is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # --- the run thread -------------------------------------------------------

    def drain(self, timeout: float | None = None) -> bool:
        """Wait for clauses still in flight after a stop; False on timeout."""
        with self._cond:
            return self._cond.wait_for(lambda: self._inflight == 0, timeout)

    def _run(self) -> None:
        # Worker threads start with an empty log context, so each run and clause binds its own.
        with bind(review_id=self.review_id, document_id=self.review.document_id):
            self._run_bound()

    def _run_bound(self) -> None:
        pool = None
        try:
            self._start_trace()
            pool = ThreadPoolExecutor(
                max_workers=self.ctx.max_concurrency, thread_name_prefix=self._thread.name
            )
            self._resolve_cached()
            finished = self._work(pool)
            if finished:
                return
            self._trace.end({"stopped": True})
            with self._finish_gate:
                self._on_closed(self)
        except Exception as exc:  # noqa: BLE001 - the review, not the process, fails
            log.exception("review %s failed", self.review_id)
            self._fail_run(exc)
        finally:
            if pool is not None:
                # Never block here: a stopped run leaves in-flight clauses to finish (or be
                # interrupted by the closed limiter) on their own; the pool queue is empty.
                pool.shutdown(wait=False, cancel_futures=True)

    def _start_trace(self) -> None:
        self._trace = start_review_trace(self.ctx.tracer, self.review, reason=self.reason)
        update(trace_id=self._trace.trace_id)
        if self._trace.trace_id and self._trace.trace_id != self.review.trace_id:
            try:
                self.ctx.store.reviews.set_trace(self.review_id, self._trace.trace_id)
                self.review.trace_id = self._trace.trace_id
            except Exception:  # noqa: BLE001 - tracing never fails a review
                log.warning("could not store the trace id of %s", self.review_id, exc_info=True)

    def announce(self) -> None:
        """Append RUN_STARTED and STATE_SNAPSHOT. The service calls it before `start`."""
        store = self.ctx.store
        with self._cond:
            stored = store.reviews.get(self.review_id)
            if stored is not None:  # timings and trace id as persisted; counts are ours
                stored.counts, stored.cost_usd = self.review.counts, self._cost
                self.review = stored
            self.review.status = "running"
            emit = self.ctx.bus.emit
            emit(
                self.review_id,
                RunStartedEvent(thread_id=self.review.document_id, run_id=self.review_id),
            )
            emit(
                self.review_id, StateSnapshotEvent(snapshot=build_state(self.review, self._entries))
            )
            self._announced = True

    def _resolve_cached(self) -> None:
        """Settle every clause whose verdict was already cached when the run opened.

        One store write for their statuses, one for the counts and one append for
        their events, however many there are: a review served entirely from the
        cache costs a handful of round trips, not several per clause. The events
        are one STATE_DELTA (a `replace` per clause plus the counts) and one
        `progress`, appended together so nothing interleaves.
        """
        with self._cond:
            batch, self._cached_batch = self._cached_batch, []
            if not batch:
                return
            if self._stopping:
                return  # stays queued for the resume
            hits = {p: self._cached_verdicts[self._clauses[p].content_hash] for p in batch}
            settled = {p: self._settled(p, verdict) for p, verdict in hits.items()}
            try:
                self.ctx.store.reviews.set_clause_statuses(
                    self.review_id, {p: (status, None) for p, status in settled.items()}
                )
            except Exception:  # noqa: BLE001 - fall back to the clause-by-clause path
                log.warning("bulk cache-hit write failed for %s", self.review_id, exc_info=True)
                self._pending.extendleft(reversed(batch))
                return
            for position, verdict in hits.items():
                self._entries[position] = (settled[position], None, verdict)
            self._cache_hits += len(hits)
            counts = self._save_counts()
            if not self._announced:
                return  # the snapshot will carry it
            delta = [
                {
                    "op": "replace",
                    "path": f"/clauses/{position}",
                    "value": clause_entry(settled[position], None, verdict),
                }
                for position, verdict in hits.items()
            ]
            delta.append(
                {"op": "replace", "path": "/review/counts", "value": counts.model_dump(mode="json")}
            )
            interrupts = [
                interrupt_event(position, verdict)
                for position, verdict in sorted(hits.items())
                if settled[position] == "awaiting_decision"
            ]
            self.ctx.bus.emit_many(
                self.review_id,
                [StateDeltaEvent(delta=delta), *interrupts, self._progress_event()],
            )

    def _work(self, pool: ThreadPoolExecutor) -> bool:
        """Feed the pool until the queue is empty. True if finished, False if stopped."""
        while True:
            with self._cond:
                if self._stopping:
                    self._closed = True
                    return False
                else:
                    while self._pending and self._inflight < self.ctx.max_concurrency:
                        position = self._pending.popleft()
                        self._inflight += 1
                        future = pool.submit(self._judge_clause, position)
                        future.add_done_callback(self._release_if_cancelled)
                idle = not self._pending and self._inflight == 0
                if not idle:
                    self._cond.wait(self.ctx.poll_seconds)
            if idle and not self._stopping:
                if self._try_finish():
                    return True
                continue
            self._watch_limiter()

    def _try_finish(self) -> bool:
        with self._finish_gate:
            with self._cond:
                if self._pending or self._inflight or self._stopping:
                    return False
                self._closed = True
                counts = count(self._entries, self._cache_hits, self._decisions)
                result = {"counts": counts.model_dump(mode="json"), "cost_usd": self._cost}
                waiting = any(s == "awaiting_decision" for s, _, _ in self._entries.values())
                if self.review.document_type == "avv":
                    self._emit_missing()
                # Status first: a client that sees RUN_FINISHED and reloads must see it.
                self.ctx.store.reviews.set_status(
                    self.review_id, "awaiting_decisions" if waiting else "finished"
                )
                self.ctx.bus.emit(
                    self.review_id,
                    RunFinishedEvent(
                        thread_id=self.review.document_id, run_id=self.review_id, result=result
                    ),
                )
            self._trace.end(result)
            self._on_closed(self)
        return True

    def _emit_missing(self) -> None:
        """The AVV document-level check, once every clause has its route and verdict:
        one `replace /missing_obligations` before the terminal event (docs/Events.md)."""
        letters = avv.mentioned(self.ctx.store, self.review, self._entries)
        missing = avv.missing_obligations(
            self.review, getattr(self.ctx.judge, "profile", None), letters
        )
        self.ctx.bus.emit(
            self.review_id,
            StateDeltaEvent(
                delta=[
                    {
                        "op": "replace",
                        "path": "/missing_obligations",
                        "value": [item.model_dump(mode="json") for item in missing],
                    }
                ]
            ),
        )

    def _fail_run(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {exc}"
        with self._finish_gate:
            with self._cond:
                self._closed = True
            try:
                self.ctx.store.reviews.set_status(self.review_id, "failed", message)
                self.ctx.bus.emit(
                    self.review_id, RunErrorEvent(message=message, code="review_failed")
                )
            except Exception:  # noqa: BLE001 - nothing left to tell
                log.exception("could not record the failure of review %s", self.review_id)
            self._trace.end(error=message)
            self._on_closed(self)

    # --- one clause (worker threads) --------------------------------------------

    def _release_if_cancelled(self, future) -> None:
        if future.cancelled():  # cancelled by a stop before it ran: the clause stays queued
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def _judge_clause(self, position: int) -> None:
        with bind(
            review_id=self.review_id,
            document_id=self.review.document_id,
            trace_id=self._trace.trace_id,
            clause=position,
        ):
            self._judge_clause_guarded(position)

    def _judge_clause_guarded(self, position: int) -> None:
        try:
            self._judge_clause_unguarded(position)
        except Exception as exc:  # noqa: BLE001 - the store, not the judge, failed
            log.exception("review %s: clause %s could not be recorded", self.review_id, position)
            try:
                with self._cond:
                    self._set(position, "failed", error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - left non-final; a resume picks it up
                log.warning("clause %s of %s left unrecorded", position, self.review_id)
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def _judge_clause_unguarded(self, position: int) -> None:
        store, judge = self.ctx.store, self.ctx.judge
        clause = self._clauses[position]
        step = f"clause:{position}"

        key = (clause.content_hash, self.review.agent_version)

        if self._record_cache_hit(position):
            return
        with self.ctx.flights.hold(key):
            # Whoever held the key may just have saved this very verdict.
            if self._record_cache_hit(position):
                return
            with self._cond:
                if self._stopping:
                    return  # stays queued for the resume
                self.ctx.bus.emit(self.review_id, StepStartedEvent(step_name=step))
                self._set(position, "checking")

            produced = None
            options = {}
            if getattr(judge, "interrupts", False):  # an uncertain verdict leaves a checkpoint
                options["thread_id"] = thread_id(self.review_id, position)
            try:
                produced = judge(clause, parent=self._trace.span, **options)
                stored = store.verdicts.save(produced)
            except Exception as exc:  # noqa: BLE001 - principle 3: the clause fails, not the run
                if interrupted(exc):
                    log.info(
                        "review %s: clause %s interrupted by shutdown", self.review_id, position
                    )
                    return  # stays `checking`; the resume judges it
                if isinstance(exc, ClauseFailed):
                    error = str(exc) or "Die Klausel konnte nicht geprüft werden."
                else:
                    error = f"{type(exc).__name__}: {exc}"
                outcome: tuple = ("failed", error)
            else:
                outcome = ("done", stored)
                self._cached_verdicts[clause.content_hash] = stored
                with self.ctx.verdict_cache_lock:
                    self.ctx.verdict_cache[key] = stored

            with self._cond:
                if outcome[0] == "done":
                    # Charge what this call cost, even if another verdict won the save.
                    self._cost += self._price(produced)
                    self._settle(position, outcome[1])
                else:
                    self._set(position, "failed", error=outcome[1])
                self.ctx.bus.emit(self.review_id, StepFinishedEvent(step_name=step))
                self._progress()

    def _record_cache_hit(self, position: int) -> bool:
        clause = self._clauses[position]
        cached = self._cached_verdicts.get(clause.content_hash)
        if cached is None:
            with self.ctx.verdict_cache_lock:
                cached = self.ctx.verdict_cache.get(
                    (clause.content_hash, self.review.agent_version)
                )
            if cached is not None:
                self._cached_verdicts[clause.content_hash] = cached
        if cached is None:
            return False
        with self._cond:
            self._cache_hits += 1
            self._settle(position, cached)
            self._progress()
        return True

    # --- helpers; callers hold self._cond -------------------------------------

    def _set(
        self,
        position: int,
        status: ClauseStatus,
        *,
        error: str | None = None,
        verdict: ClauseVerdict | None = None,
    ) -> None:
        """Change one clause: persist it, then (once announced) emit the delta."""
        self._entries[position] = (status, error, verdict)
        self.ctx.store.reviews.set_clause_status(self.review_id, position, status, error)
        counts = self._save_counts()
        if not self._announced:
            return  # the snapshot will carry it
        delta = [
            {
                "op": "replace",
                "path": f"/clauses/{position}",
                "value": clause_entry(status, error, verdict),
            },
            {"op": "replace", "path": "/review/counts", "value": counts.model_dump(mode="json")},
        ]
        self.ctx.bus.emit(self.review_id, StateDeltaEvent(delta=delta))

    def _settled(self, position: int, verdict: ClauseVerdict) -> ClauseStatus:
        return settled_status(verdict, position in self._decisions)

    def _settle(self, position: int, verdict: ClauseVerdict) -> None:
        """A verdict arrived: `done`, or `awaiting_decision` followed by its interrupt."""
        status = self._settled(position, verdict)
        self._set(position, status, verdict=verdict)
        if status == "awaiting_decision" and self._announced:
            self.ctx.bus.emit(self.review_id, interrupt_event(position, verdict))

    def _save_counts(self) -> ReviewCounts:
        counts = count(self._entries, self._cache_hits, self._decisions)
        self.review.counts = counts
        self.review.cost_usd = self._cost
        self.ctx.store.reviews.update_counts(self.review_id, counts, self._cost)
        return counts

    def _progress(self) -> None:
        self.ctx.bus.emit(self.review_id, self._progress_event())

    def _progress_event(self) -> CustomEvent:
        self._waiting_reason = self._limiter_reason()
        return CustomEvent(
            name=CUSTOM_PROGRESS,
            value={
                "counts": self.review.counts.model_dump(mode="json"),
                "cost_usd": self._cost,
                "budget_left": self._budget_left(),
                "waiting_reason": self._waiting_reason,
            },
        )

    def _watch_limiter(self) -> None:
        """Tell the UI when the shared limiter starts or stops making clauses wait."""
        reason = self._limiter_reason()
        with self._cond:
            if reason == self._waiting_reason or not self._announced or self._closed:
                return
            if self._inflight == 0 and reason is not None:
                return  # another review is the one waiting
            self._progress()

    def _limiter_reason(self) -> str | None:
        try:
            return self.ctx.limiter.waiting_reason()
        except Exception:  # noqa: BLE001 - a limiter without the hook just never says
            return None

    def _budget_left(self) -> int | None:
        try:
            return self.ctx.limiter.remaining_today()
        except Exception:  # noqa: BLE001
            return None

    def _price(self, verdict: ClauseVerdict) -> float:
        return price(self.ctx.prices, verdict.input_tokens, verdict.output_tokens)

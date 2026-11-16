"""The review runner and service against the memory store, a fake judge and fake time.

Concurrency is made deterministic with gates rather than sleeps: a gated clause's
judge call blocks until the test opens its gate, and announces through `entered`
that it has started. With `max_concurrency=1` clauses run strictly in document
order, so "clause n entered" means every event of clauses < n is in the log.
Every blocking wait has a deadline, so a regression fails instead of hanging.
"""

import queue
import threading
from collections import Counter
from datetime import UTC, datetime

import pytest
from core.budget import SharedLimiter
from core.hashing import content_hash

from app.agent.run import ClauseFailed
from app.domain import (
    EVENT_SCHEMA_VERSION,
    ClauseSpan,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Review,
)
from app.review.limiter import build_limiter, review_settings
from app.review.service import build_service
from app.store.memory import memory_store

DEADLINE = 10.0
VERSION = "agent-v1"


# --- fakes ------------------------------------------------------------------------


def _verdict(text: str, verdict: str = "valid", version: str = VERSION) -> ClauseVerdict:
    return ClauseVerdict(
        content_hash=content_hash(text),
        agent_version=version,
        verdict=verdict,
        confidence=0.9,
        input_tokens=1000,
        output_tokens=100,
    )


class FakeJudge:
    """Judges by text. `fail` maps text to how many attempts fail; `gated` texts block."""

    def __init__(
        self,
        *,
        fail: dict[str, int] | None = None,
        gated: tuple[str, ...] = (),
        limiter=None,
        verdicts: dict[str, str] | None = None,
        version: str = VERSION,
    ) -> None:
        self.agent_version = version
        self.fail = dict(fail or {})
        self.verdicts = verdicts or {}
        self.limiter = limiter
        self.gates = {text: threading.Event() for text in gated}
        self.entered = {text: threading.Event() for text in gated}
        self.calls: list[str] = []
        self.parents: list = []
        self._lock = threading.Lock()

    def __call__(self, clause: ClauseSpan, *, parent=None) -> ClauseVerdict:
        with self._lock:
            self.calls.append(clause.text)
            self.parents.append(parent)
            failing = self.fail.get(clause.text, 0) > 0
            if failing:
                self.fail[clause.text] -= 1
        if clause.text in self.gates:
            self.entered[clause.text].set()
            assert self.gates[clause.text].wait(DEADLINE), "gate never opened"
        if self.limiter is not None:
            self.limiter.acquire()
        if failing:
            if clause.text.startswith("boom"):
                raise RuntimeError("provider exploded")
            raise ClauseFailed("Gemini antwortet nicht.")
        return _verdict(clause.text, self.verdicts.get(clause.text, "valid"), self.agent_version)

    def open(self, text: str) -> None:
        self.gates[text].set()

    def wait_entered(self, text: str) -> None:
        assert self.entered[text].wait(DEADLINE), f"{text!r} was never judged"


class FakeSpan:
    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self.id = f"span-{trace_id}"
        self.ended = 0
        self.outputs: list = []

    def update(self, **kwargs) -> None:
        self.outputs.append(kwargs)

    def end(self) -> None:
        self.ended += 1


class FakeTracer:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.spans: list[FakeSpan] = []

    def start_observation(self, **kwargs) -> FakeSpan:
        self.calls.append(kwargs)
        trace = kwargs.get("trace_context", {}).get("trace_id") or f"trace-{len(self.calls)}"
        span = FakeSpan(trace)
        self.spans.append(span)
        return span


class BrokenTracer:
    def start_observation(self, **kwargs):
        raise ConnectionError("langfuse down")


class FakeClock:
    """Time that only moves when someone sleeps; shared by every worker thread."""

    def __init__(self, start: datetime) -> None:
        self.now = start.timestamp()
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += max(seconds, 0.001)


class StubLimiter:
    def __init__(self) -> None:
        self.reason: str | None = None

    def waiting_reason(self) -> str | None:
        return self.reason

    def remaining_today(self) -> int | None:
        return 42


# --- helpers ------------------------------------------------------------------------


def _document(store, texts: list[str], *, out_of_scope: tuple[int, ...] = ()) -> str:
    document_id = content_hash("\n".join(texts))
    clauses = [
        ClauseSpan(
            position=i,
            number=f"{i + 1}.",
            text=text,
            content_hash=content_hash(text),
            in_scope=i not in out_of_scope,
        )
        for i, text in enumerate(texts)
    ]
    store.documents.save(
        Document(
            document_id=document_id,
            filename="agb.pdf",
            source_type="text",
            parser_version="p",
            splitter_version="s",
            text_hash=document_id,
        ),
        clauses,
        {c.content_hash: c.text for c in clauses},
    )
    return document_id


def _service(store, judge, limiter=None, **kwargs):
    kwargs.setdefault("max_concurrency", 1)
    kwargs.setdefault("poll_seconds", 0.01)
    return build_service(store, judge, limiter or StubLimiter(), **kwargs)


def _collect(service, review_id: str, after_seq: int = 0) -> list:
    """Every event until the stream ends, read on a thread with a deadline."""
    out: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            for event in service.events(review_id, after_seq):
                out.put(event)
        finally:
            out.put(None)

    threading.Thread(target=run, daemon=True).start()
    events = []
    while (event := out.get(timeout=DEADLINE)) is not None:
        events.append(event)
    return events


def _finish(service, review_id: str) -> Review:
    assert service.wait(review_id, DEADLINE), "the run did not finish"
    return service.get(review_id)


# --- the gate tests from the brief ------------------------------------------------


def test_full_review_events_match_the_contract():
    store = memory_store()
    texts = ["cached clause", "judged clause", "failing clause"]
    store.verdicts.save(_verdict("cached clause"))
    judge = FakeJudge(fail={"failing clause": 1}, verdicts={"judged clause": "potentially_void"})
    service = _service(store, judge, prices={"input": 1.0, "output": 10.0})
    review = service.start(_document(store, texts), ["liability", "warranty"])

    events = _collect(service, review.review_id)

    def kind(event) -> str:
        if event.type == "STATE_DELTA":
            return f"DELTA {event.payload['delta'][0]['value']['status']}"
        if event.type in ("STEP_STARTED", "STEP_FINISHED"):
            return f"{event.type} {event.payload['stepName']}"
        return event.type

    assert [kind(e) for e in events] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "DELTA done",  # cache hit: no step pair
        "CUSTOM",
        "STEP_STARTED clause:1",
        "DELTA checking",
        "DELTA done",
        "STEP_FINISHED clause:1",
        "CUSTOM",
        "STEP_STARTED clause:2",
        "DELTA checking",
        "DELTA failed",
        "STEP_FINISHED clause:2",
        "CUSTOM",
        "RUN_FINISHED",
    ]
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.payload["metadata"]["schemaVersion"] == EVENT_SCHEMA_VERSION for e in events)

    started, snapshot = events[0].payload, events[1].payload["snapshot"]
    assert (started["threadId"], started["runId"]) == (review.document_id, review.review_id)
    assert snapshot["review"]["status"] == "running"
    assert {pos: c["status"] for pos, c in snapshot["clauses"].items()} == {
        "0": "queued",
        "1": "queued",
        "2": "queued",
    }
    delta = events[6].payload["delta"]
    assert [op["path"] for op in delta] == ["/clauses/1", "/review/counts"]
    assert delta[0]["value"]["verdict"]["verdict"] == "potentially_void"
    assert events[11].payload["delta"][0]["value"]["error"] == "Gemini antwortet nicht."

    progress = events[13].payload
    assert progress["name"] == "progress"
    assert set(progress["value"]) == {"counts", "cost_usd", "budget_left", "waiting_reason"}
    assert progress["value"]["budget_left"] == 42

    result = events[-1].payload["result"]
    assert result["counts"] == {
        "total": 3,
        "done": 2,
        "failed": 1,
        "potentially_void": 1,
        "uncertain": 0,
        "valid": 1,
        "out_of_scope": 0,
        "cache_hits": 1,
    }
    # Only the judged clause costs: 1000 input tokens at $1/M, 100 output at $10/M.
    assert result["cost_usd"] == pytest.approx(0.002)

    finished = _finish(service, review.review_id)
    assert finished.status == "finished"
    assert finished.counts.model_dump() == result["counts"]
    assert judge.calls == ["judged clause", "failing clause"]


def test_replaying_the_log_rebuilds_the_final_state_under_concurrency():
    """Snapshot plus deltas, applied in seq order, must equal what findings report."""
    store = memory_store()
    texts = [f"clause {i}" for i in range(30)]
    for text in texts[::3]:
        store.verdicts.save(_verdict(text))
    judge = FakeJudge(fail={t: 1 for t in texts[1::7]}, verdicts={t: "uncertain" for t in texts})
    service = _service(store, judge, max_concurrency=8)
    review = service.start(_document(store, texts), ["liability"])
    events = _collect(service, review.review_id)

    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    state = events[1].payload["snapshot"]
    for event in events[2:]:
        for op in event.payload.get("delta", []):
            _, section, key = op["path"].split("/")
            state[section][key] = op["value"]
    findings = service.findings(review.review_id)
    assert {int(p): c["status"] for p, c in state["clauses"].items()} == {
        f.position: f.status for f in findings
    }
    final = _finish(service, review.review_id)
    assert state["review"]["counts"] == final.counts.model_dump()
    assert final.counts.done + final.counts.failed == 30


def test_cache_hits_make_zero_judge_calls():
    store = memory_store()
    texts = [f"clause {i}" for i in range(5)]
    document_id = _document(store, texts)

    first = FakeJudge()
    service = _service(store, first, max_concurrency=3)
    review = service.start(document_id, ["liability"])
    assert _finish(service, review.review_id).counts.done == 5
    assert len(first.calls) == 5

    # The same document again, in a new process: every verdict comes from the cache.
    second = FakeJudge()
    again = _service(store, second, max_concurrency=3)
    rerun = again.start(document_id, ["liability"])
    assert rerun.review_id != review.review_id
    finished = _finish(again, rerun.review_id)
    assert second.calls == []
    assert finished.counts.cache_hits == 5
    assert finished.counts.done == 5
    assert finished.cost_usd == 0
    types = [e.type for e in _collect(again, rerun.review_id)]
    assert "STEP_STARTED" not in types


def test_one_failing_clause_leaves_the_others_done():
    store = memory_store()
    texts = ["ok 1", "boom", "ok 2", "bad", "ok 3"]
    judge = FakeJudge(fail={"boom": 1, "bad": 1})
    service = _service(store, judge, max_concurrency=4)
    review = service.start(_document(store, texts), ["liability"])

    finished = _finish(service, review.review_id)
    assert finished.status == "finished"
    assert (finished.counts.done, finished.counts.failed) == (3, 2)

    findings = {f.position: f for f in service.findings(review.review_id)}
    assert [findings[p].status for p in range(5)] == ["done", "failed", "done", "failed", "done"]
    assert findings[1].error == "RuntimeError: provider exploded"
    assert findings[3].error == "Gemini antwortet nicht."
    assert findings[0].verdict is not None and findings[1].verdict is None


def test_two_concurrent_reviews_share_one_limiter_and_never_exceed_it():
    clock = FakeClock(datetime(2026, 11, 16, tzinfo=UTC))
    limiter = SharedLimiter(rpm=3, daily_budget=5, clock=clock, sleep=clock.sleep)
    stamps: list[float] = []
    stamps_lock = threading.Lock()

    class RecordingLimiter:
        """Records the moment each request is granted; delegates everything else."""

        def acquire(self, n: int = 1) -> None:
            limiter.acquire(n)
            with stamps_lock:
                stamps.append(clock())

        def waiting_reason(self):
            return limiter.waiting_reason()

        def remaining_today(self):
            return limiter.remaining_today()

    shared = RecordingLimiter()
    store = memory_store()
    judge = FakeJudge(limiter=shared)
    service = _service(store, judge, shared, max_concurrency=4)
    first = service.start(_document(store, [f"a{i}" for i in range(6)]), ["liability"])
    second = service.start(_document(store, [f"b{i}" for i in range(6)]), ["warranty"])

    for review in (first, second):
        finished = _finish(service, review.review_id)
        assert (finished.status, finished.counts.done) == ("finished", 6)

    assert len(stamps) == 12
    per_day = Counter(datetime.fromtimestamp(s, UTC).date() for s in stamps)
    assert max(per_day.values()) <= 5
    assert len(per_day) >= 3  # 12 requests at 5 a day had to wait for two new days
    ordered = sorted(stamps)
    for i, start in enumerate(ordered):
        assert sum(1 for s in ordered[i:] if s < start + 60) <= 3


def test_concurrent_start_gives_one_review():
    store = memory_store()
    judge = FakeJudge(gated=("slow",))
    service = _service(store, judge)
    document_id = _document(store, ["slow", "fast"])

    barrier = threading.Barrier(8)
    ids: list[str] = []
    ids_lock = threading.Lock()

    def start() -> None:
        barrier.wait(DEADLINE)
        review = service.start(document_id, ["liability"])
        with ids_lock:
            ids.append(review.review_id)

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(DEADLINE)

    assert len(ids) == 8 and len(set(ids)) == 1
    judge.open("slow")
    assert _finish(service, ids[0]).status == "finished"
    assert judge.calls.count("slow") == 1

    # Once finished, starting again is a new review (served from the verdict cache).
    assert service.start(document_id, ["liability"]).review_id != ids[0]


def test_a_killed_run_resumes_and_judges_only_the_missing_clauses():
    store = memory_store()
    texts = ["one", "two", "three", "four"]
    document_id = _document(store, texts)
    judge = FakeJudge(gated=("two",))
    service = _service(store, judge)
    review = service.start(document_id, ["liability"])

    judge.wait_entered("two")
    service.shutdown(timeout=0)  # the process goes down while "two" is being judged
    judge.open("two")
    assert service.wait(review.review_id, DEADLINE)

    stopped = store.reviews.get(review.review_id)
    assert stopped.status == "running"  # left for the next process to resume
    statuses = {p: s for p, (s, _) in store.reviews.clause_statuses(review.review_id).items()}
    assert statuses == {0: "done", 1: "done", 2: "queued", 3: "queued"}
    with pytest.raises(RuntimeError):
        service.start(document_id, ["liability"])

    fresh_judge = FakeJudge()
    restarted = _service(store, fresh_judge, tracer=FakeTracer())
    assert restarted.resume_running() == 1
    finished = _finish(restarted, review.review_id)
    assert fresh_judge.calls == ["three", "four"]
    assert finished.status == "finished"
    assert (finished.counts.done, finished.counts.total) == (4, 4)

    events = _collect(restarted, review.review_id)
    assert [e.type for e in events].count("RUN_STARTED") == 2
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_resume_counts_a_judged_but_unrecorded_clause_as_a_cache_hit():
    store = memory_store()
    texts = ["done", "judged", "never", "failed"]
    document_id = _document(store, texts)
    review = store.reviews.create(
        Review(
            review_id="rev-1",
            document_id=document_id,
            agent_version=VERSION,
            scope=["liability"],
            status="running",
        )
    )
    store.verdicts.save(_verdict("done"))
    store.verdicts.save(_verdict("judged"))  # saved, then the process died
    store.reviews.set_clause_status("rev-1", 0, "done")
    store.reviews.set_clause_status("rev-1", 1, "checking")
    store.reviews.set_clause_status("rev-1", 2, "queued")
    store.reviews.set_clause_status("rev-1", 3, "failed", "earlier error")

    other = store.reviews.create(
        review.model_copy(update={"review_id": "rev-v0", "agent_version": "agent-v0"})
    )

    judge = FakeJudge()
    service = _service(store, judge)
    assert service.resume_running() == 1
    finished = _finish(service, "rev-1")

    assert judge.calls == ["never"]
    assert finished.counts.cache_hits == 1
    assert (finished.counts.done, finished.counts.failed) == (3, 1)
    # A review from an older agent version is not resumed under a new one.
    assert store.reviews.get(other.review_id).status == "failed"
    assert [e.type for e in store.events.since(other.review_id)] == ["RUN_ERROR"]


def test_mid_run_subscriber_and_reconnect_get_exactly_the_missed_events():
    store = memory_store()
    texts = ["first", "second", "third"]
    judge = FakeJudge(gated=tuple(texts))
    service = _service(store, judge)
    review = service.start(_document(store, texts), ["liability"])

    early: queue.Queue = queue.Queue()

    def follow() -> None:
        for event in service.events(review.review_id):
            early.put(event)
        early.put(None)

    threading.Thread(target=follow, daemon=True).start()

    judge.open("first")
    judge.wait_entered("second")  # all of clause 0's events are stored; clause 1 is live
    seen = list(store.events.since(review.review_id))
    reconnect_after = seen[3].seq  # the client saw four events, then dropped

    late_out: queue.Queue = queue.Queue()
    resumed_out: queue.Queue = queue.Queue()

    def subscribe(out: queue.Queue, after: int) -> None:
        for event in service.events(review.review_id, after):
            out.put(event)
        out.put(None)

    threading.Thread(target=subscribe, args=(late_out, 0), daemon=True).start()
    threading.Thread(target=subscribe, args=(resumed_out, reconnect_after), daemon=True).start()
    judge.open("second")
    judge.open("third")

    def drain(out: queue.Queue) -> list:
        items = []
        while (item := out.get(timeout=DEADLINE)) is not None:
            items.append(item)
        return items

    full, late, resumed = drain(early), drain(late_out), drain(resumed_out)
    assert [e.seq for e in full] == list(range(1, len(full) + 1))
    assert full[-1].type == "RUN_FINISHED"
    assert [e.model_dump() for e in late] == [e.model_dump() for e in full]

    snapshot, *missed = resumed
    assert (snapshot.seq, snapshot.type) == (0, "STATE_SNAPSHOT")
    assert snapshot.payload["snapshot"]["clauses"]["0"]["status"] == "done"
    assert [e.model_dump() for e in missed] == [
        e.model_dump() for e in full if e.seq > reconnect_after
    ]


def test_one_trace_per_review_with_every_clause_under_it():
    store = memory_store()
    tracer = FakeTracer()
    judge = FakeJudge(fail={"retry me": 1})
    service = _service(store, judge, tracer=tracer, max_concurrency=3)
    review = service.start(_document(store, ["a", "b", "retry me"]), ["liability"])
    _finish(service, review.review_id)

    assert len(tracer.spans) == 1
    root = tracer.spans[0]
    assert tracer.calls[0]["name"] == "review" and tracer.calls[0]["as_type"] == "span"
    assert judge.parents == [root, root, root]
    assert root.ended == 1
    assert store.reviews.get(review.review_id).trace_id == root.trace_id

    # A retry is another run of the same review: it joins the same trace.
    service.retry_clause(review.review_id, 2)
    _finish(service, review.review_id)
    assert tracer.calls[1]["trace_context"] == {"trace_id": root.trace_id}
    assert judge.parents[-1] is tracer.spans[1]
    assert store.reviews.get(review.review_id).trace_id == root.trace_id


def test_tracing_failure_never_fails_a_review():
    store = memory_store()
    judge = FakeJudge()
    service = _service(store, judge, tracer=BrokenTracer())
    review = service.start(_document(store, ["a", "b"]), ["liability"])
    finished = _finish(service, review.review_id)
    assert finished.status == "finished" and finished.counts.done == 2
    assert finished.trace_id is None
    assert judge.parents == [None, None]


def test_retry_of_a_failed_clause_rejudges_it_and_updates_counts():
    store = memory_store()
    judge = FakeJudge(fail={"flaky": 1})
    service = _service(store, judge)
    review = service.start(_document(store, ["steady", "flaky", "calm"]), ["liability"])
    first_pass = _collect(service, review.review_id)
    assert _finish(service, review.review_id).counts.failed == 1

    with pytest.raises(ValueError):
        service.retry_clause(review.review_id, 0)  # done, not failed
    with pytest.raises(KeyError):
        service.retry_clause("no-such-review", 1)

    service.retry_clause(review.review_id, 1)
    finished = _finish(service, review.review_id)
    assert (finished.status, finished.counts.done, finished.counts.failed) == ("finished", 3, 0)
    assert judge.calls == ["steady", "flaky", "calm", "flaky"]

    retry_events = _collect(service, review.review_id, after_seq=first_pass[-1].seq)
    assert [e.type for e in retry_events] == [
        "STATE_SNAPSHOT",  # the reconnect snapshot, seq 0
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "STEP_STARTED",
        "STATE_DELTA",
        "STATE_DELTA",
        "STEP_FINISHED",
        "CUSTOM",
        "RUN_FINISHED",
    ]
    stored = retry_events[1:]
    assert [e.seq for e in stored] == list(
        range(first_pass[-1].seq + 1, first_pass[-1].seq + 1 + len(stored))
    )
    assert stored[1].payload["snapshot"]["clauses"]["1"]["status"] == "queued"
    assert stored[-1].payload["result"]["counts"]["failed"] == 0

    # A full replay of the retried review runs through both runs.
    replay = _collect(service, review.review_id)
    assert [e.type for e in replay].count("RUN_FINISHED") == 2


def test_retry_while_the_run_is_still_going_joins_its_queue():
    store = memory_store()
    judge = FakeJudge(fail={"flaky": 1}, gated=("slow",))
    service = _service(store, judge)
    review = service.start(_document(store, ["flaky", "slow"]), ["liability"])

    judge.wait_entered("slow")  # "flaky" has failed and been recorded
    service.retry_clause(review.review_id, 0)
    with pytest.raises(ValueError):
        service.retry_clause(review.review_id, 0)  # queued again, no longer failed
    judge.open("slow")

    finished = _finish(service, review.review_id)
    assert (finished.counts.done, finished.counts.failed) == (2, 0)
    assert judge.calls == ["flaky", "slow", "flaky"]
    types = [e.type for e in _collect(service, review.review_id)]
    assert types.count("RUN_STARTED") == 1 and types.count("RUN_FINISHED") == 1


def test_progress_reports_when_the_limiter_makes_clauses_wait():
    store = memory_store()
    limiter = StubLimiter()
    judge = FakeJudge(gated=("waits",))
    service = _service(store, judge, limiter)
    review = service.start(_document(store, ["waits"]), ["liability"])

    out: queue.Queue = queue.Queue()

    def follow() -> None:
        for event in service.events(review.review_id):
            out.put(event)
        out.put(None)

    threading.Thread(target=follow, daemon=True).start()
    judge.wait_entered("waits")
    limiter.reason = "daily_budget"

    def next_progress() -> dict:
        while True:
            event = out.get(timeout=DEADLINE)
            assert event is not None, "stream ended before the progress event"
            if event.type == "CUSTOM":
                return event.payload["value"]

    assert next_progress()["waiting_reason"] == "daily_budget"
    limiter.reason = None
    assert next_progress()["waiting_reason"] is None
    judge.open("waits")
    assert _finish(service, review.review_id).counts.done == 1


def test_findings_carry_status_verdict_decision_feedback_and_scope():
    store = memory_store()
    judge = FakeJudge(fail={"bad": 1})
    service = _service(store, judge)
    document_id = _document(store, ["good", "bad", "aside"], out_of_scope=(2,))
    review = service.start(document_id, ["liability"])
    _finish(service, review.review_id)
    store.decisions.put(Decision(review_id=review.review_id, position=0, action="accept"))
    store.feedback.put(
        Feedback(
            feedback_id="f1",
            review_id=review.review_id,
            position=1,
            agent_version=VERSION,
            vote="down",
        )
    )

    findings = service.findings(review.review_id)
    assert [(f.position, f.status) for f in findings] == [
        (0, "done"),
        (1, "failed"),
        (2, "out_of_scope"),
    ]
    assert findings[0].verdict.verdict == "valid" and findings[0].decision.action == "accept"
    assert findings[1].feedback.vote == "down" and findings[1].error
    assert judge.calls == ["good", "bad"]
    counts = service.get(review.review_id).counts
    assert (counts.total, counts.out_of_scope) == (2, 1)
    with pytest.raises(KeyError):
        service.findings("no-such-review")
    with pytest.raises(KeyError):
        service.events("no-such-review")


def test_an_error_outside_any_clause_fails_the_review_with_run_error():
    store = memory_store()
    real_append = store.events.append

    def append(review_id, type, payload):
        if type == "RUN_STARTED":
            raise ConnectionError("database went away")
        return real_append(review_id, type, payload)

    store.events.append = append
    service = _service(store, FakeJudge())
    review = service.start(_document(store, ["a"]), ["liability"])

    events = _collect(service, review.review_id)
    assert [e.type for e in events] == ["RUN_ERROR"]
    assert events[0].payload["code"] == "review_failed"
    failed = _finish(service, review.review_id)
    assert failed.status == "failed" and "database went away" in failed.error


def test_start_rejects_unknown_documents():
    service = _service(memory_store(), FakeJudge())
    with pytest.raises(KeyError):
        service.start("missing", ["liability"])


def test_limiter_and_settings_come_from_config():
    config = {
        "review": {
            "rpm": 7,
            "daily_budget": 100,
            "max_concurrency": 2,
            "price_usd_per_million_tokens": {"input": 0.5},
        }
    }
    limiter = build_limiter(config)
    assert (limiter.rpm, limiter.daily_budget) == (7, 100)
    assert review_settings(config) == {
        "max_concurrency": 2,
        "prices": {"input": 0.5, "output": 0.0},
    }
    assert review_settings({}) == {"max_concurrency": 4, "prices": {"input": 0.0, "output": 0.0}}

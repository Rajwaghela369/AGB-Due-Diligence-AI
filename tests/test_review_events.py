"""The event bus: persist before notify, gap-free subscriptions, and no lost wake-ups.

Every blocking read runs on a helper thread with a deadline, so a regression shows
up as a failed assertion rather than a hung test run.
"""

import queue
import threading

from ag_ui.core import RunFinishedEvent, RunStartedEvent, StepStartedEvent

from app.domain import EVENT_SCHEMA_VERSION
from app.review.events import EventBus, serialise
from app.store.memory import memory_store

DEADLINE = 10.0


def _consume(iterator) -> tuple[queue.Queue, threading.Thread]:
    """Drain `iterator` on a thread; items go to the queue, then a final `None`."""
    out: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            for item in iterator:
                out.put(item)
        finally:
            out.put(None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return out, thread


def _drain(out: queue.Queue) -> list:
    items = []
    while (item := out.get(timeout=DEADLINE)) is not None:
        items.append(item)
    return items


def _started() -> RunStartedEvent:
    return RunStartedEvent(thread_id="doc", run_id="rev")


def _finished() -> RunFinishedEvent:
    return RunFinishedEvent(thread_id="doc", run_id="rev", result={})


def test_serialise_is_camel_case_with_schema_version():
    payload = serialise(_started())
    assert payload == {
        "type": "RUN_STARTED",
        "threadId": "doc",
        "runId": "rev",
        "metadata": {"schemaVersion": EVENT_SCHEMA_VERSION},
    }


def test_emit_stores_before_anyone_reads():
    store = memory_store()
    bus = EventBus(store)
    stored = bus.emit("rev", _started())
    assert stored.seq == 1
    assert [e.type for e in store.events.since("rev")] == ["RUN_STARTED"]


def test_replay_stops_at_the_terminal_event():
    store = memory_store()
    bus = EventBus(store)
    for event in (_started(), StepStartedEvent(step_name="clause:0"), _finished()):
        bus.emit("rev", event)
    assert [e.seq for e in bus.subscribe("rev")] == [1, 2, 3]
    assert [e.seq for e in bus.subscribe("rev", after_seq=1)] == [2, 3]


def test_replay_carries_on_past_a_finish_that_is_not_the_last_event():
    # A retried review stores RUN_STARTED ... RUN_FINISHED again under the same id.
    store = memory_store()
    bus = EventBus(store)
    for event in (_started(), _finished(), _started(), _finished()):
        bus.emit("rev", event)
    assert [e.seq for e in bus.subscribe("rev")] == [1, 2, 3, 4]


def test_live_subscriber_gets_every_event_exactly_once():
    store = memory_store()
    bus = EventBus(store, recheck_seconds=3600)
    bus.emit("rev", _started())
    out, thread = _consume(bus.subscribe("rev"))
    assert out.get(timeout=DEADLINE).seq == 1  # replayed, now blocking for live events

    emitters = [
        threading.Thread(
            target=lambda i=i: bus.emit("rev", StepStartedEvent(step_name=f"clause:{i}"))
        )
        for i in range(20)
    ]
    for emitter in emitters:
        emitter.start()
    for emitter in emitters:
        emitter.join()
    bus.emit("rev", _finished())

    rest = _drain(out)
    thread.join(DEADLINE)
    assert [e.seq for e in rest] == list(range(2, 23))
    assert bus.subscriber_count("rev") == 0


def test_event_appended_between_read_and_wait_is_not_missed():
    """The subscribe-after-register race, forced: the append lands right after the read.

    Without the version check the subscriber would sleep for `recheck_seconds`
    (an hour here) and the deadline below would fail the test.
    """
    store = memory_store()
    bus = EventBus(store, recheck_seconds=3600)
    bus.emit("rev", _started())

    real_since = store.events.since
    raced = threading.Event()

    def since_then_append(review_id, after_seq=0):
        events = list(real_since(review_id, after_seq))
        if not raced.is_set():
            raced.set()
            bus.emit(review_id, _finished())  # lands after the read, before the wait
        return iter(events)

    store.events.since = since_then_append
    out, thread = _consume(bus.subscribe("rev"))
    events = _drain(out)
    thread.join(DEADLINE)
    assert [(e.seq, e.type) for e in events] == [(1, "RUN_STARTED"), (2, "RUN_FINISHED")]


def test_shutdown_wakes_blocked_subscribers():
    store = memory_store()
    bus = EventBus(store, recheck_seconds=3600)
    bus.emit("rev", _started())
    out, thread = _consume(bus.subscribe("rev"))
    assert out.get(timeout=DEADLINE).seq == 1
    bus.shutdown()
    assert out.get(timeout=DEADLINE) is None
    thread.join(DEADLINE)
    assert not thread.is_alive()


def test_idle_timeout_and_no_follow_end_the_stream():
    store = memory_store()
    bus = EventBus(store, recheck_seconds=3600)
    bus.emit("rev", _started())
    assert [e.seq for e in bus.subscribe("rev", follow=False)] == [1]
    assert [e.seq for e in bus.subscribe("rev", idle_timeout=0)] == [1]

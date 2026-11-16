"""The review event log and its in-process fan-out (docs/Events.md).

Every event is appended to `run_events` *before* anyone is told about it, so the
store is the only source of truth: a subscriber never receives an event that a
reload could not also read. The in-process part is only a doorbell. A
subscriber registers on a review's channel, notes the channel's version, reads
the store, and only then waits for the version to move. An event appended after
that read bumps the version after the note was taken, so the wait returns at
once; an event appended before the read is already in what was read. Either way
nothing is lost, and de-duplicating by `seq` means nothing is sent twice.

Appends to one review are serialised here as well as in the store, so `seq`
order is emission order and a reader never sees seq n+1 committed while n is
still in flight. Across processes (Phase 9) the doorbell becomes LISTEN/NOTIFY;
until then a periodic re-read of the store is the safety net.
"""

import threading
import time
from collections.abc import Iterator
from typing import Any

from ag_ui.core import BaseEvent
from core.logging import get_logger

from app.domain import EVENT_SCHEMA_VERSION, StoredEvent
from app.store.interfaces import Store

log = get_logger("review.events")

TERMINAL_TYPES = frozenset({"RUN_FINISHED", "RUN_ERROR"})


def serialise(event: BaseEvent) -> dict[str, Any]:
    """The stored and streamed form: camelCase AG-UI fields plus `metadata.schemaVersion`."""
    metadata = {**(event.metadata or {}), "schemaVersion": EVENT_SCHEMA_VERSION}
    stamped = event.model_copy(update={"metadata": metadata})
    return stamped.model_dump(by_alias=True, exclude_none=True, mode="json")


class _Channel:
    """One review's doorbell. `version` moves on every append; `closed` on shutdown."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.version = 0
        self.subscribers = 0


class EventBus:
    """Persist-then-notify event log with blocking, gap-free subscriptions.

    `recheck_seconds` bounds how long a subscriber sleeps without re-reading the
    store; it only matters if an event was appended by another process.
    """

    def __init__(self, store: Store, *, recheck_seconds: float = 5.0) -> None:
        self._store = store
        self._recheck = recheck_seconds
        self._lock = threading.Lock()  # guards the two dicts and _closed
        self._channels: dict[str, _Channel] = {}
        self._append_locks: dict[str, threading.Lock] = {}
        self._closed = False

    # --- emitting -----------------------------------------------------------

    def emit(self, review_id: str, event: BaseEvent) -> StoredEvent:
        payload = serialise(event)
        with self._lock:
            append_lock = self._append_locks.setdefault(review_id, threading.Lock())
        with append_lock:
            stored = self._store.events.append(review_id, payload["type"], payload)
        self._ring(review_id)
        return stored

    def _ring(self, review_id: str) -> None:
        with self._lock:
            channel = self._channels.get(review_id)
        if channel is None:
            return  # nobody listening; a later subscriber reads the store first
        with channel.cond:
            channel.version += 1
            channel.cond.notify_all()

    # --- subscribing --------------------------------------------------------

    def subscribe(
        self,
        review_id: str,
        after_seq: int = 0,
        *,
        follow: bool = True,
        idle_timeout: float | None = None,
    ) -> Iterator[StoredEvent]:
        """Stored events with seq > after_seq, then live ones, each exactly once.

        Stops after a RUN_FINISHED or RUN_ERROR that is the newest stored event
        (a retried review stores more events after its first RUN_FINISHED, and a
        replay must carry on through them). Also stops when `follow` is False
        and the store is drained, after `idle_timeout` seconds without a new
        event, or when the bus shuts down.
        """
        channel = self._register(review_id)
        last = after_seq
        idle_since = time.monotonic()
        try:
            while True:
                with channel.cond:
                    seen_version = channel.version
                batch = [e for e in self._store.events.since(review_id, last) if e.seq > last]
                for event in batch:
                    yield event
                    last = event.seq
                if batch:
                    idle_since = time.monotonic()
                    if batch[-1].type in TERMINAL_TYPES:
                        return
                if not follow:
                    return

                with channel.cond:
                    while channel.version == seen_version and not self._closed:
                        wait = self._recheck
                        if idle_timeout is not None:
                            left = idle_timeout - (time.monotonic() - idle_since)
                            if left <= 0:
                                return
                            wait = min(wait, left)
                        if not channel.cond.wait(wait):
                            break  # re-read the store in case another process appended
                    if self._closed:
                        return
        finally:
            self._unregister(review_id, channel)

    def _register(self, review_id: str) -> _Channel:
        with self._lock:
            channel = self._channels.setdefault(review_id, _Channel())
            channel.subscribers += 1
            return channel

    def _unregister(self, review_id: str, channel: _Channel) -> None:
        with self._lock:
            channel.subscribers -= 1
            if channel.subscribers == 0 and self._channels.get(review_id) is channel:
                del self._channels[review_id]

    def subscriber_count(self, review_id: str) -> int:
        with self._lock:
            channel = self._channels.get(review_id)
            return channel.subscribers if channel else 0

    def shutdown(self) -> None:
        """Wake every blocked subscriber and end its stream; later waits return at once."""
        with self._lock:
            self._closed = True
            channels = list(self._channels.values())
        for channel in channels:
            with channel.cond:
                channel.cond.notify_all()

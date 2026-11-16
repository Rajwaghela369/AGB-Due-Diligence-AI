"""Bridge the review service's blocking event iterator into an async SSE response."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress

from fastapi import Request

from app.domain import StoredEvent

PING_SECONDS = 15.0
_DONE = object()


def encode_event(event: StoredEvent) -> bytes:
    lines = []
    if event.seq > 0:
        lines.append(f"id: {event.seq}")
    lines.extend(
        [
            f"event: {event.type}",
            f"data: {json.dumps(event.payload, ensure_ascii=False, separators=(',', ':'))}",
            "",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


async def stream_events(request: Request, events: Iterator[StoredEvent]) -> AsyncIterator[bytes]:
    output: queue.Queue[StoredEvent | BaseException | object] = queue.Queue(maxsize=1)
    stopped = threading.Event()

    def offer(item: StoredEvent | BaseException | object) -> bool:
        while not stopped.is_set():
            try:
                output.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def produce() -> None:
        try:
            for event in events:
                if not offer(event):
                    return
        except BaseException as exc:  # passed back to the response task
            offer(exc)
        finally:
            offer(_DONE)

    producer = threading.Thread(target=produce, name="review-sse", daemon=True)
    producer.start()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PING_SECONDS
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                item = output.get_nowait()
            except queue.Empty:
                now = loop.time()
                if now >= deadline:
                    yield b": ping\n\n"
                    deadline = now + PING_SECONDS
                await asyncio.sleep(0.05)
                continue
            if item is _DONE:
                return
            if isinstance(item, BaseException):
                raise item
            yield encode_event(item)
            deadline = loop.time() + PING_SECONDS
    finally:
        stopped.set()
        close = getattr(events, "close", None)
        if callable(close):
            with suppress(RuntimeError, ValueError):
                close()
        producer.join(timeout=1)

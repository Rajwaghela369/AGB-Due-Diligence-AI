"""One logger factory, so pipeline runs and the app log the same way.

Pipelines and evals keep the human-readable format. The API, and any process started
with `LOG_FORMAT=json`, writes one JSON object per line instead. Every line carries the
fields bound with `bind()`, read when the line is emitted: the HTTP middleware binds
`request_id`, and the review runner binds `review_id`, `document_id` and `trace_id`, so a
single request can be followed from its log lines to its Langfuse trace.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from types import MappingProxyType
from typing import IO, Any

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

# Chatty on every HTTP call and every dataset resolution step; our own logs
# are unreadable otherwise.
_NOISY = ("httpx", "httpcore", "datasets", "fsspec", "filelock", "urllib3")

# Attributes every LogRecord has. Anything else on a record came from `extra=`.
_STANDARD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_context: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "log_context", default=_EMPTY
)
_handler: logging.Handler | None = None
_configured = False


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add fields to every log line emitted inside the block, in this context only."""
    token = _context.set({**_context.get(), **_present(fields)})
    try:
        yield
    finally:
        _context.reset(token)


def update(**fields: Any) -> None:
    """Add fields for the rest of the enclosing `bind()` block, e.g. once a trace id is known."""
    _context.set({**_context.get(), **_present(fields)})


def _present(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_context.get())
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(*, json_logs: bool, stream: IO[str] | None = None) -> None:
    """Install the one handler this module owns on the root logger, replacing any earlier one."""
    global _handler, _configured
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(os.getenv("AGB_LOG_LEVEL", "INFO"))
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _handler = handler
    _configured = True


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        # Like basicConfig: if something (Prefect, pytest) already owns the root logger, leave it.
        if logging.getLogger().handlers:
            for noisy in _NOISY:
                logging.getLogger(noisy).setLevel(logging.WARNING)
            _configured = True
        else:
            configure(json_logs=os.getenv("LOG_FORMAT", "").lower() == "json")
    return logging.getLogger(name)

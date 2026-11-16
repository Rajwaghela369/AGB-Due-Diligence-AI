"""One Langfuse trace per review, and never a reason for a review to fail.

The review's root observation is the explicit `parent` of every clause judge
call: worker threads do not inherit OpenTelemetry context, so the clause spans
would otherwise each start a trace of their own. A resumed or retried run
re-attaches to the review's existing trace id, so a review stays one trace
however many times its process restarts.

Every call into the tracer is guarded. Langfuse being down, misconfigured or
absent (`tracer=None`) costs the trace, not the review.
"""

from contextlib import suppress
from typing import Any

from core.logging import get_logger

from app.domain import Review

log = get_logger("review.tracing")


class ReviewTrace:
    """The root span of one run of a review; `span` is None when there is no tracing."""

    def __init__(self, span: Any = None) -> None:
        self.span = span

    @property
    def trace_id(self) -> str | None:
        return getattr(self.span, "trace_id", None) if self.span is not None else None

    def end(self, output: dict[str, Any] | None = None, *, error: str | None = None) -> None:
        if self.span is None:
            return
        try:
            if error is not None:
                self.span.update(output=output, level="ERROR", status_message=error)
            elif output is not None:
                self.span.update(output=output)
        except Exception:  # noqa: BLE001 - tracing never fails a review
            log.warning("could not record the review span output", exc_info=True)
        try:
            self.span.end()
        except Exception:  # noqa: BLE001
            log.warning("could not end the review span", exc_info=True)


def start_review_trace(tracer: Any, review: Review, *, reason: str = "start") -> ReviewTrace:
    """Open the review's root span, joining its existing trace when it has one."""
    if tracer is None:
        return ReviewTrace()
    kwargs: dict[str, Any] = {
        "name": "review",
        "as_type": "span",
        "input": {"document_id": review.document_id, "scope": list(review.scope)},
        "metadata": {
            "review_id": review.review_id,
            "agent_version": review.agent_version,
            "run": reason,
        },
    }
    span = None
    if review.trace_id:
        with suppress(Exception):
            span = tracer.start_observation(trace_context={"trace_id": review.trace_id}, **kwargs)
    if span is None:
        try:
            span = tracer.start_observation(**kwargs)
        except Exception:  # noqa: BLE001 - tracing never fails a review
            log.warning("could not open the review trace", exc_info=True)
            return ReviewTrace()
    return ReviewTrace(span)

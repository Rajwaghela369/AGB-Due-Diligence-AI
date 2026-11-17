"""The graph's state and its per-invocation context.

State is what the checkpointer saves when a clause waits for the lawyer, so it holds
only data. The tracer is a live client object; it travels in the invocation context,
which LangGraph hands to the nodes but never checkpoints.
"""

from dataclasses import dataclass
from typing import Any, TypedDict

from core.retrieval import Retrieved


class AgentState(TypedDict, total=False):
    clause: dict[str, Any]
    candidates: list[Retrieved]
    # The router's topic, kept so a resume can widen the search the way a retry does.
    topic: str
    # Everything the judge decided, before self_check has had its say.
    draft: dict[str, Any]
    retries: int
    # Which self_check rule fired, carried so the trace can show the reason.
    check: str
    # The step observations collected for Phase 5 feedback attribution.
    observations: dict[str, str | None]
    # True once a lawyer's decision resumed the interrupted clause (Phase 7).
    decided: bool
    result: dict[str, Any]


@dataclass(frozen=True)
class AgentContext:
    """Per-invocation values that must never be checkpointed."""

    # A per-invocation tracer lets one compiled graph run beneath each clause span.
    tracer: Any = None

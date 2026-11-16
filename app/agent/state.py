"""The graph's state. Keys are written by one node and read by the next."""

from typing import Any, TypedDict

from core.retrieval import Retrieved


class AgentState(TypedDict, total=False):
    clause: dict[str, Any]
    candidates: list[Retrieved]
    # Everything the judge decided, before self_check has had its say.
    draft: dict[str, Any]
    retries: int
    # Which self_check rule fired, carried so the trace can show the reason.
    check: str
    result: dict[str, Any]

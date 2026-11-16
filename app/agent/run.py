"""Judge one clause for the service. The only entry point `app/review` calls.

The agent still knows nothing of documents, reviews or HTTP: it gets one
clause's text and a parent observation, and returns one verdict. Built once per
process and called from worker threads, so it must be thread-safe.

Contract (Phase 5, Wave 0):

    judge = make_judge(app_config=None, *, limiter, client=None, retriever=None,
                       router=None, tracer=None, caches=None)
    judge.agent_version            # core of the verdict cache key
    judge(clause, parent=span)     # -> ClauseVerdict, or raises ClauseFailed

- `clause` is an `app.domain.ClauseSpan`; only `text`, `heading` and
  `content_hash` are read.
- `parent` is a Langfuse observation (the review's root span) or None. The judge
  opens one `agent` observation named `clause <number or position>` beneath it via
  `parent.start_as_current_observation(...)`, with the existing step
  observations (route, retrieve, judge, self-check, retry-retrieve, explain)
  nested inside, and returns their ids in `ClauseVerdict.observations`.
  Worker threads do not inherit OpenTelemetry context, which is why the parent
  is passed explicitly rather than taken from the current context.
- `limiter` is a `core.budget.SharedLimiter`; every provider request reserves
  from it first. Running out of budget blocks (with the reason exposed by the
  limiter), it does not fail the clause.
- `caches` is an `app.store.interfaces.CacheRepo`-shaped object (or None) used
  for embedding and retrieval lookups; injected so `app/agent` never imports
  `app/store`.
- A judge error (provider failure after retries, no verdict) raises
  `ClauseFailed`. An explanation failure does not: the verdict and basis are
  kept, `reason_de=""`, `reason_unavailable=True`.
- Nothing is persisted here; the runner owns persistence.
"""

from typing import Any, Protocol

from app.domain import ClauseSpan, ClauseVerdict


class ClauseFailed(RuntimeError):
    """The clause could not be judged. The message is shown to the lawyer."""


class Judge(Protocol):
    agent_version: str

    def __call__(self, clause: ClauseSpan, *, parent: Any = None) -> ClauseVerdict: ...


def make_judge(
    app_config: dict[str, Any] | None = None,
    *,
    limiter: Any,
    client: Any = None,
    retriever: Any = None,
    router: Any = None,
    tracer: Any = None,
    caches: Any = None,
) -> Judge:
    raise NotImplementedError("Phase 5 Wave 1: core-hooks")

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

import time
from contextlib import nullcontext, suppress
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
    """Build one thread-safe clause judge for the review runner.

    The compiled LangGraph is immutable; per-clause tracing is supplied in its
    invocation state, so worker threads do not share trace context.
    """
    from core.config import load_yaml
    from core.llm import complete, decide
    from core.paths import CONFIGS
    from core.retrieval import Retrieved
    from core.retrieval import retrieve as default_retrieve
    from core.routing import classify_topic

    from app.agent import prompts, verdicts
    from app.agent.graph import build_graph
    from app.agent.task import _agent_settings, _explain_model, index_hash

    app_config = app_config or load_yaml(CONFIGS / "app.yaml")
    label = app_config.get("prompts", {}).get("label", "production")
    agent_settings = _agent_settings(app_config)
    judge_model = app_config.get("models", {}).get("judge", {})
    router_model = app_config.get("models", {}).get("router", {})
    explain_model = _explain_model(app_config)
    version = verdicts.agent_version(app_config, index_hash(), prompts.fingerprint(label))

    def decide_clause(text: str, questions: dict[str, dict], model: dict[str, Any]) -> Any:
        return decide(text, questions, model or judge_model, budget=limiter, client=client)

    def route_clause(text: str) -> str:
        if router is not None:
            return router(text)
        return classify_topic(text, model=router_model.get("name", "jina-embeddings-v3"))

    def retrieve_clause(text: str, topic: str, policy: dict[str, Any], *, depth: int) -> Any:
        if retriever is not None:
            return retriever(text, topic, policy, depth=depth)
        return default_retrieve(text, topic, policy, depth=depth, cache=caches)

    def explain_clause(clause: dict[str, Any], cited: list[Retrieved]) -> Any:
        evidence = "\n\n".join(f"{candidate.citation}\n{candidate.text}" for candidate in cited)
        return complete(
            prompts.load("explain", label=label).text,
            prompts.explain_message(clause.get("title"), clause["text"], evidence),
            prompts.reason_schema(),
            explain_model,
            budget=limiter,
            client=client,
        )

    compiled = build_graph(
        router=route_clause,
        decider=decide_clause,
        explainer=explain_clause,
        retriever=retrieve_clause,
        policy=app_config.get("retrieval", {}),
        models={"judge": judge_model, "explain": explain_model},
        prompt_label=label,
        tracer=tracer,
        **agent_settings,
    )

    def judge_clause(clause: ClauseSpan, *, parent: Any = None) -> ClauseVerdict:
        span_tracer = parent or tracer
        name = f"clause {clause.number or clause.position}"
        context = nullcontext(None)
        if span_tracer is not None:
            with suppress(Exception):  # tracing cannot prevent a review
                context = span_tracer.start_as_current_observation(
                    name=name,
                    as_type="agent",
                    input={"heading": clause.heading, "text": clause.text},
                    metadata={"agent_version": version, "content_hash": clause.content_hash},
                )

        started = time.monotonic()
        try:
            with context as clause_observation:
                state = compiled.invoke(
                    {
                        "clause": {
                            "clause_id": clause.content_hash,
                            "title": clause.heading,
                            "text": clause.text,
                        },
                        "retries": 0,
                        "tracer": clause_observation or tracer,
                    }
                )
                result = state["result"]
                if result.get("verdict") is None:
                    raise ClauseFailed(str(result.get("error") or "no verdict returned"))
                observation_ids = dict(result.get("observations", {}))
                observation_ids["clause_span"] = getattr(clause_observation, "id", None)
                verdict = ClauseVerdict(
                    content_hash=clause.content_hash,
                    agent_version=version,
                    verdict=result["verdict"],
                    legal_basis=result.get("legal_basis", []),
                    candidates=result.get("candidates", []),
                    basis_scores=result.get("basis_scores", {}),
                    reason_de=result.get("reason_de", ""),
                    reason_unavailable=bool(result.get("reason_unavailable", False)),
                    confidence=float(result.get("confidence", 0.0)),
                    void_probability=result.get("void_probability"),
                    check=result.get("check"),
                    # A span has no get_current_trace_id(); it carries its trace id.
                    trace_id=result.get("langfuse_trace_id")
                    or getattr(clause_observation, "trace_id", None),
                    observations=observation_ids,
                    input_tokens=int(result.get("input_tokens", 0)),
                    output_tokens=int(result.get("output_tokens", 0)),
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
                if clause_observation is not None:
                    with suppress(Exception):
                        clause_observation.update(output=verdict.model_dump(mode="json"))
                return verdict
        except ClauseFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - runner needs a clause-scoped failure
            raise ClauseFailed(f"{type(exc).__name__}: {exc}") from exc

    judge_clause.agent_version = version  # type: ignore[attr-defined]
    return judge_clause  # type: ignore[return-value]

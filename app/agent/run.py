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

Interrupts (Phase 7). `make_judge(..., checkpointer=saver)` compiles the graph with a
LangGraph checkpointer, and `judge.interrupts` is then True: the runner passes
`thread_id="{review_id}:{position}"`, and an `uncertain` verdict stops the graph after
`self_check` with its state saved under that thread. The judge still returns the
model's verdict at once; the clause simply has a live checkpoint now. Later,
`judge.resume(clause, thread_id=..., verdict=..., basis=...)` continues it with the
lawyer's decision (explaining on the lawyer's § if the decided verdict is
potentially_void) and returns a `Resumed`, or None when the thread has nothing waiting
(a cached verdict, or a checkpointer-less judge). Checkpoints are written once per
invocation (`durability="exit"`), and a thread that ran to the end is deleted, so only
waiting clauses keep a checkpoint.
"""

import time
import uuid
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from typing import Any, Protocol

from core.logging import get_logger

from app.domain import ClauseSpan, ClauseVerdict

log = get_logger("agent.run")


class ClauseFailed(RuntimeError):
    """The clause could not be judged. The message is shown to the lawyer."""


class ExplainFailed(ClauseFailed):
    """The explanation could not be regenerated; the stored verdict is untouched."""


@dataclass(frozen=True)
class Resumed:
    """What resuming an interrupted clause produced. Tokens are this resume's only."""

    verdict: str | None  # the verdict the graph finished with (the lawyer's, unless dismissed)
    explained: bool  # the explain step ran (a model call was attempted)
    reason_de: str = ""
    reason_unavailable: bool = False
    input_tokens: int = 0
    output_tokens: int = 0


class Judge(Protocol):
    agent_version: str
    interrupts: bool  # True when compiled with a checkpointer; then pass `thread_id`

    def __call__(
        self, clause: ClauseSpan, *, parent: Any = None, thread_id: str | None = None
    ) -> ClauseVerdict: ...

    def resume(
        self, clause: ClauseSpan, *, thread_id: str, verdict: str | None, basis: list[str]
    ) -> "Resumed | None":
        """Continue the interrupted graph of `thread_id` with the lawyer's verdict and §.

        None if nothing waits on that thread. Raises only for an infrastructure failure
        (the checkpointer); a failed explanation is `reason_unavailable=True`.
        """
        ...

    def explain_only(self, clause: ClauseSpan, verdict: ClauseVerdict) -> ClauseVerdict:
        """Re-run only the explanation of a void verdict whose reasoning is missing.

        One Gemini call through the limiter, retried on 429/5xx by `core.llm`. Returns
        the verdict with `reason_de` set and the extra tokens added; raises `ExplainFailed`.
        """
        ...


def make_judge(
    app_config: dict[str, Any] | None = None,
    *,
    limiter: Any,
    client: Any = None,
    retriever: Any = None,
    router: Any = None,
    tracer: Any = None,
    caches: Any = None,
    checkpointer: Any = None,
) -> Judge:
    """Build one thread-safe clause judge for the review runner.

    The compiled LangGraph is immutable; per-clause tracing is supplied in its
    invocation state, so worker threads do not share trace context.
    """
    from core import masking
    from core.budget import BudgetExceeded
    from core.config import load_yaml
    from core.llm import complete, decide
    from core.paths import CONFIGS
    from core.retrieval import Retrieved
    from core.retrieval import retrieve as default_retrieve
    from core.routing import classify_topic
    from langgraph.types import Command

    from app.agent import prompts, verdicts
    from app.agent.graph import build_graph, find_cited
    from app.agent.state import AgentContext
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
        checkpointer=checkpointer,
        **agent_settings,
    )

    def forget(thread_id: str) -> None:
        """Drop a thread that has nothing left to resume; a leftover costs only space."""
        try:
            checkpointer.delete_thread(thread_id)
        except Exception:  # noqa: BLE001 - never fails a verdict
            log.warning("could not delete checkpoint thread %s", thread_id, exc_info=True)

    def clause_scope(clause: ClauseSpan, subject: str | None) -> Any:
        """Phase 9 (9.A): one masking scope per clause. Primed with the clause text first
        (so its placeholders are numbered the same for every owner and every cached
        embedding and verdict), then the heading. Answers may only get back values the
        clause text itself contains: the verdict is cached for every document with this
        text, and a name from one owner's heading must not reach another owner."""
        return masking.scope(
            None,
            subject or clause.content_hash,
            prime=[clause.text, clause.heading],
            restore_from=[clause.text],
        )

    def judge_clause(
        clause: ClauseSpan, *, parent: Any = None, thread_id: str | None = None
    ) -> ClauseVerdict:
        with clause_scope(clause, thread_id):
            return _judge_clause(clause, parent=parent, thread_id=thread_id)

    def _judge_clause(
        clause: ClauseSpan, *, parent: Any = None, thread_id: str | None = None
    ) -> ClauseVerdict:
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

        def inside(clause_observation: Any) -> ClauseVerdict:
            config, durable = None, {}
            if checkpointer is not None:
                config = {"configurable": {"thread_id": thread_id or uuid.uuid4().hex}}
                durable = {"durability": "exit"}  # one checkpoint write, not one per step
            state = compiled.invoke(
                {
                    "clause": {
                        "clause_id": clause.content_hash,
                        "title": clause.heading,
                        "text": clause.text,
                    },
                    "retries": 0,
                },
                config,
                context=AgentContext(tracer=clause_observation or tracer),
                **durable,
            )
            if "__interrupt__" in state:  # uncertain: the checkpoint waits for the lawyer
                result = state["__interrupt__"][0].value
            else:
                result = state["result"]
                if config is not None:
                    forget(config["configurable"]["thread_id"])
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

        failure: Exception | None = None
        verdict: ClauseVerdict | None = None
        try:
            with context as clause_observation:
                # Phase 9 (9.A): an exception leaving the observation is recorded by
                # OpenTelemetry with its message and stack, which no mask hook sees. So it
                # is caught inside and raised again outside; the span gets only its type.
                try:
                    verdict = inside(clause_observation)
                except Exception as exc:  # noqa: BLE001 - re-raised below
                    failure = exc
                    if clause_observation is not None:
                        with suppress(Exception):
                            clause_observation.update(
                                level="ERROR", status_message=type(exc).__name__
                            )
            if failure is not None:
                raise failure
            assert verdict is not None
            return verdict
        except ClauseFailed:
            raise
        except Exception as exc:  # noqa: BLE001 - runner needs a clause-scoped failure
            raise ClauseFailed(f"{type(exc).__name__}: {exc}") from exc

    def explain_only(clause: ClauseSpan, verdict: ClauseVerdict) -> ClauseVerdict:
        """The explain step alone: find the cited norms again (cached, no Gemini), then explain."""
        with clause_scope(clause, None):
            return _explain_only(clause, verdict)

    def _explain_only(clause: ClauseSpan, verdict: ClauseVerdict) -> ClauseVerdict:
        retrieval = app_config.get("retrieval", {})
        try:
            cited = find_cited(
                retrieve_clause,
                clause.text,
                route_clause(clause.text),
                retrieval,
                list(verdict.legal_basis),
            )
            if not cited:
                raise ExplainFailed("Die Rechtsgrundlage wurde nicht mehr gefunden.")
            completion = explain_clause({"title": clause.heading, "text": clause.text}, cited)
        except (ExplainFailed, BudgetExceeded):
            raise  # a closed limiter (shutdown) is not a provider failure
        except Exception as exc:  # noqa: BLE001 - retrieval or routing broke
            raise ExplainFailed(f"{type(exc).__name__}: {exc}") from exc
        if completion.error:
            raise ExplainFailed(str(completion.error))
        reason = prompts.read_reason(completion.text)
        if not reason:
            raise ExplainFailed("Das Modell lieferte keine Begründung.")
        return verdict.model_copy(
            update={
                "reason_de": reason,
                "reason_unavailable": False,
                "input_tokens": verdict.input_tokens + completion.input_tokens,
                "output_tokens": verdict.output_tokens + completion.output_tokens,
            }
        )

    def resume(
        clause: ClauseSpan, *, thread_id: str, verdict: str | None, basis: list[str]
    ) -> Resumed | None:
        if checkpointer is None:
            return None
        with clause_scope(clause, thread_id):
            return _resume(thread_id, verdict, basis)

    def _resume(thread_id: str, verdict: str | None, basis: list[str]) -> Resumed | None:
        config = {"configurable": {"thread_id": thread_id}}
        waiting = compiled.get_state(config)
        if not waiting.next:  # never interrupted, already resumed, or deleted
            return None
        before = waiting.values.get("draft") or {}
        state = compiled.invoke(
            Command(resume={"verdict": verdict, "basis": list(basis)}),
            config,
            context=AgentContext(tracer=tracer),
            durability="exit",
        )
        result = state["result"]
        forget(thread_id)
        return Resumed(
            verdict=result.get("verdict"),
            explained=bool((state.get("draft") or {}).get("explained")),
            reason_de=result.get("reason_de", ""),
            reason_unavailable=bool(result.get("reason_unavailable", False)),
            input_tokens=int(result.get("input_tokens", 0)) - int(before.get("input_tokens", 0)),
            output_tokens=int(result.get("output_tokens", 0)) - int(before.get("output_tokens", 0)),
        )

    judge_clause.agent_version = version  # type: ignore[attr-defined]
    judge_clause.interrupts = checkpointer is not None  # type: ignore[attr-defined]
    judge_clause.explain_only = explain_only  # type: ignore[attr-defined]
    judge_clause.resume = resume  # type: ignore[attr-defined]
    return judge_clause  # type: ignore[return-value]

"""The headless Phase 4 graph: route, retrieve, judge, check, maybe retry, explain, finish.

Built on the Gemini Decisions API, which answers typed questions with calibrated
probabilities and generates no text. That shapes the design in three ways.

One call per clause. The API evaluates up to 1000 questions against one shared
input in a single forward pass, with latency nearly flat as questions are added
and the shared input billed once. So the judge asks "is this void?" and "does
chunk X make it void?" for every candidate together, rather than one call per
question.

Grounding cannot be hallucinated. The candidate chunk ids come from our
retriever and become the question keys, so a citation we never showed the model
has no way into the answer. The old design asked a text model to pick from an
enum and checked afterwards; here there is nothing to check.

Confidence is calibrated by the API rather than self-reported by a model, which
is what finally makes the `uncertain` threshold mean something.

The router is an embedding classifier, not an LLM call, and its topic is only a
hint: `general` chunks and the AGB-control paragraphs (§§ 305-310) are always in
the candidate set, so a mis-routed clause costs ranking, not evidence. Searching
every chunk with no topic filter was measured and ranked worse (primary@5 0.258
against 0.355). Nothing in this module reads the clause's own `topics` field -
those labels are the annotators' answer key, not an input the agent is allowed
to see.

Waiting for the lawyer (Phase 7). Compiled with a checkpointer, the graph stops
after `self_check` when the verdict is `uncertain`: an `await_decision` node calls
LangGraph's `interrupt()` and the state is checkpointed under the caller's thread id.
`interrupt()` inside a node was chosen over `interrupt_after=["self_check"]` because
the stop is conditional (only `uncertain` waits) and because the node is also where
the lawyer's answer comes back in: `Command(resume={"verdict", "basis"})` makes
`interrupt()` return it, the node writes it into the draft, and the graph carries
on exactly as for a model verdict - a decided `potentially_void` goes through
`explain` (on the lawyer's §), everything else straight to `finish`. Without a
checkpointer (offline evals, the harness) there is no `await_decision` edge at all
and the graph behaves as it always did.
"""

import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, suppress
from typing import Any

from core.llm import DecisionResult, decide
from core.retrieval import Retrieved, retrieve
from core.schemas import VerdictRecord
from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agent import prompts
from app.agent.state import AgentContext, AgentState

# The AGB-control block. Always reachable on a retry, because these are the
# norms that void a clause regardless of what the clause is about.
AGB_CONTROL_PARAGRAPHS = (305, "305a", "305b", "305c", 306, "306a", 307, 308, 309, 310)

VOID_QUESTION = "is_void"


def widened(policy: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The wider net of a retry: control norms in, twice the depth, half as many more.

    One definition for the graph's retry and for every later search for a cited norm
    (explain only, a resumed decision), so they all look where the judge looked.
    """
    depth = int(policy.get("search_depth", 20))
    top_k = int(policy.get("top_k", 10))
    wider = {
        **policy,
        "always_include_paragraphs": list(AGB_CONTROL_PARAGRAPHS),
        "search_depth": depth * 2,
    }
    return wider, int(top_k * 1.5)


def find_cited(
    retriever: Callable,
    text: str,
    topic: str,
    policy: dict[str, Any],
    wanted: list[str],
    *,
    have: list[Retrieved] = (),  # type: ignore[assignment]
) -> list[Retrieved]:
    """The retrieved chunks for `wanted` ids, searching only for those not in `have`.

    A model's basis is always among its candidates, so the live path never searches. A
    lawyer may cite a § the judge never saw, and an explain-only call has no candidates
    at all: those search the way the judge did (topic first, then the retry's wider net).
    Ids that cannot be found are left out.
    """
    wanted_ids = set(wanted)
    found = {c.chunk_id: c for c in have if c.chunk_id in wanted_ids}
    if len(found) < len(wanted_ids):
        depth = int(policy.get("search_depth", 20))
        top_k = int(policy.get("top_k", 10))
        for candidate in retriever(text, topic, policy, depth=depth)[:top_k]:
            if candidate.chunk_id in wanted_ids:
                found.setdefault(candidate.chunk_id, candidate)
    if len(found) < len(wanted_ids):
        wider, wider_k = widened(policy)
        for candidate in retriever(text, "", wider, depth=wider["search_depth"])[:wider_k]:
            if candidate.chunk_id in wanted_ids:
                found.setdefault(candidate.chunk_id, candidate)
    return list(found.values())


@contextmanager
def _observe(tracer: Any, name: str, *, as_type: str = "span", **kwargs: Any):
    """Open a Langfuse observation, or do nothing when tracing is off.

    Offline tests pass `tracer=None`, and a tracer that raises must never cost
    a clause its verdict - so both paths fall back to a null context.
    """
    if tracer is None:
        with nullcontext() as observation:
            yield observation
        return
    try:
        context = tracer.start_as_current_observation(name=name, as_type=as_type, **kwargs)
    except Exception:  # noqa: BLE001 - observability is never load-bearing
        context = nullcontext()
    with context as observation:
        yield observation


def _update(observation: Any, **kwargs: Any) -> None:
    """Best-effort span update. A tracing failure must not surface as an error."""
    if observation is None:
        return
    with suppress(Exception):  # tracing is never load-bearing
        observation.update(**kwargs)


def _observation_id(observation: Any) -> str | None:
    """Read an SDK observation id without making tracing load-bearing."""
    return getattr(observation, "id", None) if observation is not None else None


def build_questions(candidates: list[Retrieved], label: str = "production") -> dict[str, dict]:
    """One void question plus one question per candidate chunk.

    Chunk ids double as question keys, which the API restricts to
    [A-Za-z0-9_-]{1,64}. Our ids look like `bgb-309-7-a` and already comply;
    anything that does not is a bug in the chunker rather than something to
    paper over here.
    """
    void_text = prompts.load("void-question", label=label).text
    basis_template = prompts.load("basis-question", label=label).text

    questions: dict[str, dict] = {
        VOID_QUESTION: {
            "type": "noul",
            "instructions": void_text,
            "criteria": {
                "true_description": (
                    "Die Klausel ist nach deutschem AGB-Recht ganz oder teilweise unwirksam."
                ),
                "false_description": "Die Klausel ist wirksam.",
            },
        }
    }
    for candidate in candidates:
        if not prompts.valid_question_key(candidate.chunk_id):
            raise ValueError(f"chunk id {candidate.chunk_id!r} is not a valid question key")
        questions[candidate.chunk_id] = {
            "type": "noul",
            "instructions": basis_template.format(citation=candidate.citation, text=candidate.text),
            "criteria": {
                "true_description": f"Die Unwirksamkeit ergibt sich aus {candidate.citation}.",
                "false_description": (
                    f"{candidate.citation} führt nicht zur Unwirksamkeit dieser Klausel."
                ),
            },
        }
    return questions


def read_answers(
    result: DecisionResult,
    candidates: list[Retrieved],
    void_threshold: float,
    basis_threshold: float,
) -> dict[str, Any]:
    """Turn calibrated probabilities into a verdict and its legal basis."""
    void_answer = result.answers[VOID_QUESTION]
    probability = float(void_answer.value)

    scored = [
        (candidate.chunk_id, float(result.answers[candidate.chunk_id].value))
        for candidate in candidates
        if candidate.chunk_id in result.answers
    ]
    # Strongest basis first, so a reviewer reads the best citation at the top.
    scored.sort(key=lambda pair: pair[1], reverse=True)

    return {
        "verdict": "potentially_void" if probability >= void_threshold else "valid",
        "void_probability": probability,
        "legal_basis": [chunk_id for chunk_id, score in scored if score >= basis_threshold],
        "basis_scores": dict(scored),
        "confidence": float(void_answer.confidence),
        "reason_de": "",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
    }


def build_graph(
    *,
    router: Callable | None = None,
    decider: Callable = decide,
    explainer: Callable | None = None,
    retriever: Callable = retrieve,
    persist: Callable | None = None,
    policy: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
    void_threshold: float = 0.5,
    basis_threshold: float = 0.5,
    confidence_uncertain_below: float = 0.6,
    max_retrieval_retries: int = 1,
    explain_enabled: bool = True,
    prompt_label: str = "production",
    run_id: str | None = None,
    tracer: Any = None,
    checkpointer: Any = None,
):
    """Compile the graph once. Every dependency is injectable so tests run offline.

    With a `checkpointer`, an `uncertain` verdict interrupts (see the module docstring);
    invoke it with `config={"configurable": {"thread_id": ...}}`. Without one, nothing
    interrupts.
    """
    policy = policy or {}
    models = models or {}
    run_id = run_id or str(uuid.uuid4())
    top_k = int(policy.get("top_k", 10))
    depth = int(policy.get("search_depth", 20))
    interrupts = checkpointer is not None

    def invocation_tracer(runtime: Runtime[AgentContext] | None) -> Any:
        context = runtime.context if runtime is not None else None
        if context is not None and context.tracer is not None:
            return context.tracer
        return tracer

    def route(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        clause = state["clause"]
        with _observe(
            invocation_tracer(runtime), "route", input={"clause": clause["text"]}
        ) as observation:
            topic = router(clause["text"]) if router else ""
            _update(observation, output={"topic": topic})
        return {"topic": topic}

    def get_candidates(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        clause = state["clause"]
        with _observe(
            invocation_tracer(runtime),
            "retrieve",
            as_type="retriever",
            input={"query": clause["text"]},
            metadata={"policy": policy, "depth": depth},
        ) as observation:
            found = retriever(clause["text"], state.get("topic", ""), policy, depth=depth)[:top_k]
            _update(
                observation,
                output=[
                    {
                        "chunk_id": candidate.chunk_id,
                        "citation": candidate.citation,
                        "score": candidate.score,
                        "rank": candidate.rank,
                    }
                    for candidate in found
                ],
            )
        return {
            "candidates": found,
            "observations": {
                **state.get("observations", {}),
                "retrieval": _observation_id(observation),
            },
        }

    def judge(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        clause, candidates = state["clause"], state["candidates"]
        questions = build_questions(candidates, prompt_label)
        with _observe(
            invocation_tracer(runtime),
            "judge",
            as_type="generation",
            input={
                "clause": clause["text"],
                "candidates": [candidate.chunk_id for candidate in candidates],
                "questions": sorted(questions),
            },
            model=models.get("judge", {}).get("name"),
        ) as observation:
            result = decider(clause["text"], questions, models.get("judge", {}))
            if result.error:
                _update(observation, output={"error": result.error}, level="WARNING")
                return {
                    "draft": {
                        "verdict": None,
                        "error": result.error,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                    }
                }

            draft = read_answers(result, candidates, void_threshold, basis_threshold)
            # A retry judges a second time; the clause's cost is the sum.
            previous = state.get("draft") or {}
            draft["input_tokens"] += int(previous.get("input_tokens", 0))
            draft["output_tokens"] += int(previous.get("output_tokens", 0))
            _update(
                observation,
                output=draft,
                model=result.model,
                usage_details={
                    "input": result.input_tokens,
                    "output": result.output_tokens,
                    "total": result.input_tokens + result.output_tokens,
                },
            )
        return {
            "draft": draft,
            "observations": {
                **state.get("observations", {}),
                "judge": _observation_id(observation),
            },
        }

    def self_check(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        """Deterministic. Retry an ungrounded void call first, abstain second.

        Order matters: a void draft with no basis and low confidence should go
        back for more evidence rather than abstain on the first look.
        """
        draft = dict(state["draft"])
        retries = int(state.get("retries", 0))

        if draft.get("error") or draft.get("verdict") is None:
            check = "failed"
        elif (
            draft["verdict"] == "potentially_void"
            and not draft["legal_basis"]
            and retries < max_retrieval_retries
        ):
            check = "retry-ungrounded"
        elif float(draft.get("confidence", 0.0)) < confidence_uncertain_below:
            draft["verdict"] = "uncertain"
            check = "abstain-low-confidence"
        elif draft["verdict"] == "potentially_void" and not draft["legal_basis"]:
            # Out of retries and still ungrounded: abstain rather than assert.
            draft["verdict"] = "uncertain"
            check = "abstain-ungrounded"
        else:
            check = "accept"

        with _observe(
            invocation_tracer(runtime), "self-check", input=state["draft"]
        ) as observation:
            _update(observation, output={"rule": check, "draft": draft})
        return {"draft": draft, "check": check}

    def explain_or_finish(state: AgentState) -> str:
        draft = state["draft"]
        if draft.get("verdict") == "potentially_void" and explain_enabled and explainer:
            return "explain"
        return "finish"

    def after_check(state: AgentState) -> str:
        if state["check"] == "retry-ungrounded":
            return "retry"
        if interrupts and state["draft"].get("verdict") == "uncertain":
            return "await_decision"
        return explain_or_finish(state)

    def await_decision(state: AgentState) -> AgentState:
        """Wait for the lawyer. Runs twice: once to interrupt, once when resumed.

        The interrupt's value is the model's result, so the caller gets its verdict
        without the graph having finished. On resume, `interrupt()` returns the decided
        `{"verdict", "basis"}`; a dismissal (verdict None) keeps the model's draft and
        only closes the thread.
        """
        decided = interrupt(summarise(state))
        draft = dict(state["draft"])
        if decided.get("verdict") is not None:
            draft["verdict"] = decided["verdict"]
            draft["legal_basis"] = list(decided.get("basis") or [])
            draft["reason_de"] = ""
        return {"draft": draft, "decided": True}

    def retry(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        """Widen the net once: deeper search, more candidates, control norms in."""
        wider, wider_k = widened(policy)
        with _observe(
            invocation_tracer(runtime),
            "retry-retrieve",
            as_type="retriever",
            input={"query": state["clause"]["text"]},
            metadata={"policy": wider, "top_k": wider_k},
        ) as observation:
            found = retriever(state["clause"]["text"], "", wider, depth=wider["search_depth"])
            found = found[:wider_k]
            _update(observation, output=[candidate.chunk_id for candidate in found])
        return {
            "candidates": found,
            "retries": int(state.get("retries", 0)) + 1,
            "observations": {
                **state.get("observations", {}),
                "retrieval": _observation_id(observation),
            },
        }

    def explain(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        """Generate the German reason. The Decisions API cannot, so this is a text call."""
        draft = dict(state["draft"])
        clause = state["clause"]
        cited = find_cited(
            retriever,
            clause["text"],
            state.get("topic", ""),
            policy,
            draft["legal_basis"],
            have=state.get("candidates", []),
        )
        with _observe(
            invocation_tracer(runtime),
            "explain",
            as_type="generation",
            input={"legal_basis": draft["legal_basis"], "decided": bool(state.get("decided"))},
            model=models.get("explain", {}).get("name"),
        ) as observation:
            draft["explained"] = True
            completion = explainer(clause, cited) if cited else None
            if completion is None:
                # Nothing to explain from (a lawyer's § that cannot be found): no call.
                draft["reason_unavailable"] = True
                _update(observation, output={"error": "no cited norm found"}, level="WARNING")
            elif completion.error:
                # A missing reason is cosmetic; the verdict and its basis stand.
                draft["reason_unavailable"] = True
                _update(observation, output={"error": completion.error}, level="WARNING")
            else:
                draft["reason_de"] = prompts.read_reason(completion.text)
                draft["reason_unavailable"] = False
                draft["input_tokens"] = int(draft.get("input_tokens", 0)) + completion.input_tokens
                draft["output_tokens"] = (
                    int(draft.get("output_tokens", 0)) + completion.output_tokens
                )
                _update(
                    observation,
                    output=draft["reason_de"],
                    model=completion.model,
                    usage_details={
                        "input": completion.input_tokens,
                        "output": completion.output_tokens,
                        "total": completion.input_tokens + completion.output_tokens,
                    },
                )
        return {
            "draft": draft,
            "observations": {
                **state.get("observations", {}),
                "explanation": _observation_id(observation),
            },
        }

    def finish(state: AgentState, runtime: Runtime[AgentContext]) -> AgentState:
        result = summarise(state)
        trace_id = None
        active_tracer = invocation_tracer(runtime)
        if active_tracer is not None:
            try:
                trace_id = active_tracer.get_current_trace_id()
            except Exception:  # noqa: BLE001
                trace_id = None

        if persist and result["verdict"] is not None:
            persist(
                VerdictRecord(
                    run_id=run_id,
                    clause_id=state["clause"]["clause_id"],
                    verdict=result["verdict"],
                    legal_basis=result["legal_basis"],
                    reason_de=result["reason_de"],
                    confidence=result["confidence"],
                    langfuse_trace_id=trace_id,
                )
            )
        result["langfuse_trace_id"] = trace_id
        return {"result": result}

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("route", route)
    graph.add_node("retrieve", get_candidates)
    graph.add_node("judge", judge)
    graph.add_node("self_check", self_check)
    graph.add_node("retry", retry)
    graph.add_node("explain", explain)
    graph.add_node("finish", finish)

    graph.set_entry_point("route")
    graph.add_edge("route", "retrieve")
    graph.add_edge("retrieve", "judge")
    graph.add_edge("judge", "self_check")
    after_check_targets = {"retry": "retry", "explain": "explain", "finish": "finish"}
    if interrupts:
        graph.add_node("await_decision", await_decision)
        after_check_targets["await_decision"] = "await_decision"
        graph.add_conditional_edges(
            "await_decision", explain_or_finish, {"explain": "explain", "finish": "finish"}
        )
    graph.add_conditional_edges("self_check", after_check, after_check_targets)
    graph.add_edge("retry", "judge")
    graph.add_edge("explain", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def summarise(state: AgentState) -> dict[str, Any]:
    """The clause's result as the graph's output contract has it (without the trace id).

    Also the value of an interrupt, so a waiting clause reports the model's verdict in
    exactly the shape a finished one does.
    """
    draft = state.get("draft") or {}
    return {
        "verdict": draft.get("verdict"),
        "legal_basis": list(draft.get("legal_basis", [])),
        "basis_scores": dict(draft.get("basis_scores", {})),
        "candidates": [candidate.chunk_id for candidate in state.get("candidates", [])],
        "reason_de": draft.get("reason_de", ""),
        "confidence": float(draft.get("confidence", 0.0)),
        "void_probability": draft.get("void_probability"),
        "error": draft.get("error"),
        "check": state.get("check"),
        "input_tokens": int(draft.get("input_tokens", 0)),
        "output_tokens": int(draft.get("output_tokens", 0)),
        "reason_unavailable": bool(draft.get("reason_unavailable", False)),
        "observations": dict(state.get("observations", {})),
    }

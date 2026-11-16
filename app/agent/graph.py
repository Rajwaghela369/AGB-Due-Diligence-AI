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
"""

import uuid
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, suppress
from typing import Any

from core.llm import DecisionResult, decide
from core.retrieval import Retrieved, retrieve
from core.schemas import VerdictRecord
from langgraph.graph import END, StateGraph

from app.agent import prompts
from app.agent.state import AgentState

# The AGB-control block. Always reachable on a retry, because these are the
# norms that void a clause regardless of what the clause is about.
AGB_CONTROL_PARAGRAPHS = (305, "305a", "305b", "305c", 306, "306a", 307, 308, 309, 310)

VOID_QUESTION = "is_void"


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
):
    """Compile the graph once. Every dependency is injectable so tests run offline."""
    policy = policy or {}
    models = models or {}
    run_id = run_id or str(uuid.uuid4())
    top_k = int(policy.get("top_k", 10))
    depth = int(policy.get("search_depth", 20))

    def route(state: AgentState) -> AgentState:
        clause = state["clause"]
        with _observe(tracer, "route", input={"clause": clause["text"]}) as observation:
            topic = router(clause["text"]) if router else ""
            _update(observation, output={"topic": topic})
        return {"topic": topic}

    def get_candidates(state: AgentState) -> AgentState:
        clause = state["clause"]
        with _observe(
            tracer,
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
        return {"candidates": found}

    def judge(state: AgentState) -> AgentState:
        clause, candidates = state["clause"], state["candidates"]
        questions = build_questions(candidates, prompt_label)
        with _observe(
            tracer,
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
        return {"draft": draft}

    def self_check(state: AgentState) -> AgentState:
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

        with _observe(tracer, "self-check", input=state["draft"]) as observation:
            _update(observation, output={"rule": check, "draft": draft})
        return {"draft": draft, "check": check}

    def after_check(state: AgentState) -> str:
        if state["check"] == "retry-ungrounded":
            return "retry"
        draft = state["draft"]
        if draft.get("verdict") == "potentially_void" and explain_enabled and explainer:
            return "explain"
        return "finish"

    def retry(state: AgentState) -> AgentState:
        """Widen the net once: deeper search, more candidates, control norms in."""
        wider = {
            **policy,
            "always_include_paragraphs": list(AGB_CONTROL_PARAGRAPHS),
            "search_depth": depth * 2,
        }
        wider_k = int(top_k * 1.5)
        with _observe(
            tracer,
            "retry-retrieve",
            as_type="retriever",
            input={"query": state["clause"]["text"]},
            metadata={"policy": wider, "top_k": wider_k},
        ) as observation:
            found = retriever(state["clause"]["text"], "", wider, depth=wider["search_depth"])
            found = found[:wider_k]
            _update(observation, output=[candidate.chunk_id for candidate in found])
        return {"candidates": found, "retries": int(state.get("retries", 0)) + 1}

    def explain(state: AgentState) -> AgentState:
        """Generate the German reason. The Decisions API cannot, so this is a text call."""
        draft = dict(state["draft"])
        cited = [c for c in state["candidates"] if c.chunk_id in draft["legal_basis"]]
        with _observe(
            tracer,
            "explain",
            as_type="generation",
            input={"legal_basis": draft["legal_basis"]},
            model=models.get("explain", {}).get("name"),
        ) as observation:
            completion = explainer(state["clause"], cited)
            if completion.error:
                # A missing reason is cosmetic; the verdict and its basis stand.
                _update(observation, output={"error": completion.error}, level="WARNING")
            else:
                draft["reason_de"] = prompts.read_reason(completion.text)
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
        return {"draft": draft}

    def finish(state: AgentState) -> AgentState:
        draft = state["draft"]
        candidates = [candidate.chunk_id for candidate in state.get("candidates", [])]
        result = {
            "verdict": draft.get("verdict"),
            "legal_basis": list(draft.get("legal_basis", [])),
            "candidates": candidates,
            "reason_de": draft.get("reason_de", ""),
            "confidence": float(draft.get("confidence", 0.0)),
            "void_probability": draft.get("void_probability"),
            "error": draft.get("error"),
            "check": state.get("check"),
            "input_tokens": int(draft.get("input_tokens", 0)),
            "output_tokens": int(draft.get("output_tokens", 0)),
        }
        trace_id = None
        if tracer is not None:
            try:
                trace_id = tracer.get_current_trace_id()
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

    graph = StateGraph(AgentState)
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
    graph.add_conditional_edges(
        "self_check",
        after_check,
        {"retry": "retry", "explain": "explain", "finish": "finish"},
    )
    graph.add_edge("retry", "judge")
    graph.add_edge("explain", "finish")
    graph.add_edge("finish", END)
    return graph.compile()

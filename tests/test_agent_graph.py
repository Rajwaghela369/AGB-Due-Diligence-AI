"""The graph's branching, proved offline with fake providers.

Every dependency is injected, so none of these tests needs Qdrant, Gemini or
Langfuse. The previous build of this graph shipped with no tests at all and
failed on 55% of a live run.
"""

from types import SimpleNamespace

import pytest
from core.llm import Answer, DecisionResult
from core.retrieval import Retrieved

from app.agent.graph import build_graph, build_questions, read_answers

CLAUSE = {
    "clause_id": "agb-349",
    "title": "6 Haftung",
    "text": "Das Recht auf Schadenersatz wird beschränkt auf Vorsatz oder grobe Fahrlässigkeit.",
    # The annotators' answer key. The agent must never read it.
    "topics": ["liability"],
}

POLICY = {"top_k": 2, "search_depth": 10, "always_include_topics": ["general"]}


def _chunk(chunk_id: str, rank: int = 1) -> Retrieved:
    return Retrieved(
        chunk_id=chunk_id,
        citation=f"§ {chunk_id}",
        text=f"Gesetzestext {chunk_id}",
        score=1.0 / rank,
        rank=rank,
    )


def _decision(void: float, confidence: float, bases: dict[str, float], **kwargs) -> DecisionResult:
    answers = {"is_void": Answer(value=void, confidence=confidence, probabilities=None)}
    for chunk_id, score in bases.items():
        answers[chunk_id] = Answer(value=score, confidence=score, probabilities=None)
    return DecisionResult(
        answers=answers,
        input_tokens=kwargs.get("input_tokens", 100),
        output_tokens=0,
        model="gemini-decision-1",
        error=kwargs.get("error"),
    )


def _run(decider, *, retriever=None, explainer=None, **overrides):
    chunks = overrides.pop("chunks", [_chunk("bgb-309-7-a")])
    retriever = retriever or (lambda text, topic, policy, depth=20: chunks)
    kwargs = {
        "decider": decider,
        "retriever": retriever,
        "explainer": explainer,
        "policy": POLICY,
        "explain_enabled": explainer is not None,
        **overrides,
    }
    graph = build_graph(**kwargs)
    return graph.invoke({"clause": dict(CLAUSE), "retries": 0})["result"]


def test_void_above_threshold_is_grounded_and_ordered_by_probability():
    chunks = [_chunk("bgb-309-7-a", 1), _chunk("bgb-307-abs1", 2), _chunk("bgb-276-abs1", 3)]
    # The weaker basis is retrieved first; probability decides the order.
    decision = _decision(0.95, 0.95, {"bgb-309-7-a": 0.6, "bgb-307-abs1": 0.9, "bgb-276-abs1": 0.1})

    result = _run(lambda *a, **k: decision, chunks=chunks, policy={**POLICY, "top_k": 3})

    assert result["verdict"] == "potentially_void"
    assert result["legal_basis"] == ["bgb-307-abs1", "bgb-309-7-a"]
    assert result["candidates"] == ["bgb-309-7-a", "bgb-307-abs1", "bgb-276-abs1"]
    assert result["void_probability"] == 0.95
    assert result["check"] == "accept"


def test_graph_reuses_a_review_topic_without_routing_again():
    seen = []
    clause = {**CLAUSE, "topic": "warranty"}

    def retriever(text, topic, policy, depth=20):
        seen.append(topic)
        return [_chunk("bgb-309-7-a")]

    graph = build_graph(
        router=lambda _text: pytest.fail("stored review topic should avoid a second route call"),
        decider=lambda *args, **kwargs: _decision(0.2, 0.9, {}),
        retriever=retriever,
        policy=POLICY,
    )
    result = graph.invoke({"clause": clause, "retries": 0})["result"]

    assert result["verdict"] == "valid"
    assert seen == ["warranty"]


def test_void_below_threshold_is_valid():
    result = _run(lambda *a, **k: _decision(0.2, 0.8, {"bgb-309-7-a": 0.1}))

    assert result["verdict"] == "valid"
    assert result["legal_basis"] == []


def test_empty_basis_retries_once_then_finishes_on_the_wider_set():
    seen = []

    def retriever(text, topic, policy, depth=20):
        seen.append({"depth": depth, "paragraphs": policy.get("always_include_paragraphs")})
        if len(seen) == 1:
            return [_chunk("bgb-276-abs1")]
        return [_chunk("bgb-309-7-a"), _chunk("bgb-307-abs1", 2)]

    calls = []

    def decider(text, questions, model):
        calls.append(sorted(questions))
        if len(calls) == 1:
            # Void, but nothing in the candidate set explains why.
            return _decision(0.9, 0.9, {"bgb-276-abs1": 0.1})
        return _decision(0.9, 0.9, {"bgb-309-7-a": 0.8, "bgb-307-abs1": 0.2})

    result = _run(decider, retriever=retriever, max_retrieval_retries=1)

    assert len(seen) == 2, "the ungrounded void verdict should have triggered one retry"
    assert seen[1]["depth"] == POLICY["search_depth"] * 2
    assert seen[1]["paragraphs"], "the retry must force the AGB-control paragraphs in"
    # The second judgement saw the widened candidate set.
    assert "bgb-309-7-a" in calls[1]
    assert result["verdict"] == "potentially_void"
    assert result["legal_basis"] == ["bgb-309-7-a"]


def test_retry_is_not_attempted_twice():
    retrievals = []

    def retriever(text, topic, policy, depth=20):
        retrievals.append(depth)
        return [_chunk("bgb-276-abs1")]

    # Always void, never grounded.
    result = _run(
        lambda *a, **k: _decision(0.9, 0.9, {"bgb-276-abs1": 0.05}),
        retriever=retriever,
        max_retrieval_retries=1,
    )

    assert len(retrievals) == 2, "one initial retrieval plus exactly one retry"
    # Out of retries and still ungrounded: abstain rather than assert.
    assert result["verdict"] == "uncertain"
    assert result["check"] == "abstain-ungrounded"


def test_retry_rule_is_checked_before_the_confidence_rule():
    """A low-confidence, ungrounded void call must retry, not abstain immediately.

    The previous build applied the confidence rule first, so these clauses
    abstained without ever asking for more evidence.
    """
    retrievals = []

    def retriever(text, topic, policy, depth=20):
        retrievals.append(depth)
        return [_chunk("bgb-276-abs1")]

    _run(
        lambda *a, **k: _decision(0.9, 0.3, {"bgb-276-abs1": 0.05}),
        retriever=retriever,
        confidence_uncertain_below=0.6,
        max_retrieval_retries=1,
    )

    assert len(retrievals) == 2, "the retry rule must win over the abstention rule"


def test_low_confidence_abstains_when_the_verdict_is_grounded():
    result = _run(
        lambda *a, **k: _decision(0.9, 0.4, {"bgb-309-7-a": 0.9}),
        confidence_uncertain_below=0.6,
    )

    assert result["verdict"] == "uncertain"
    assert result["check"] == "abstain-low-confidence"
    # The basis survives the abstention, so a reviewer still sees the evidence.
    assert result["legal_basis"] == ["bgb-309-7-a"]


def test_a_provider_error_yields_a_null_verdict_without_raising():
    failed = DecisionResult(
        answers={}, input_tokens=0, output_tokens=0, model="m", error="ServerError: 503"
    )

    result = _run(lambda *a, **k: failed)

    assert result["verdict"] is None
    assert "503" in result["error"]
    assert result["check"] == "failed"


def test_explain_disabled_leaves_the_reason_empty_and_the_verdict_intact():
    result = _run(lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}), explainer=None)

    assert result["verdict"] == "potentially_void"
    assert result["reason_de"] == ""


def test_explain_failure_does_not_cost_the_verdict():
    broken = SimpleNamespace(text="", input_tokens=0, output_tokens=0, model="m", error="RateLimit")

    result = _run(
        lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}),
        explainer=lambda clause, cited: broken,
    )

    assert result["verdict"] == "potentially_void"
    assert result["legal_basis"] == ["bgb-309-7-a"]
    assert result["reason_de"] == ""


def test_explain_fills_the_reason_and_only_sees_cited_chunks():
    cited_seen = []

    def explainer(clause, cited):
        cited_seen.append([c.chunk_id for c in cited])
        return SimpleNamespace(
            text='{"reason_de": "Verstoß gegen § 309 Nr. 7."}',
            input_tokens=50,
            output_tokens=20,
            model="gemini-flash",
            error=None,
        )

    chunks = [_chunk("bgb-309-7-a", 1), _chunk("bgb-276-abs1", 2)]
    result = _run(
        lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9, "bgb-276-abs1": 0.1}),
        explainer=explainer,
        chunks=chunks,
        policy={**POLICY, "top_k": 2},
    )

    assert result["reason_de"] == "Verstoß gegen § 309 Nr. 7."
    # The explainer is given the cited norms, not every candidate.
    assert cited_seen == [["bgb-309-7-a"]]
    # Judge tokens plus explain tokens.
    assert result["input_tokens"] == 150
    assert result["output_tokens"] == 20


def test_the_agent_never_reads_the_annotators_topics():
    """The clause carries topics=['liability']; retrieval must not receive it.

    Those labels are AGB-DE's answer key. A real client's AGB arrives untagged,
    so an agent that reads them would score well here and fail in production.
    """
    topics_seen = []

    def retriever(text, topic, policy, depth=20):
        topics_seen.append(topic)
        return [_chunk("bgb-309-7-a")]

    _run(lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}), retriever=retriever)

    assert topics_seen == [""], f"retrieval was routed by {topics_seen!r}"


def test_the_graph_runs_without_a_tracer():
    result = _run(lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}), tracer=None)

    assert result["verdict"] == "potentially_void"
    assert result["langfuse_trace_id"] is None


def test_a_broken_tracer_does_not_cost_the_verdict():
    class Exploding:
        def start_as_current_observation(self, **kwargs):
            raise RuntimeError("langfuse down")

        def get_current_trace_id(self):
            raise RuntimeError("langfuse down")

    result = _run(lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}), tracer=Exploding())

    assert result["verdict"] == "potentially_void"
    assert result["langfuse_trace_id"] is None


def test_persist_is_skipped_for_a_failed_clause():
    saved = []
    failed = DecisionResult(answers={}, input_tokens=0, output_tokens=0, model="m", error="boom")

    _run(lambda *a, **k: failed, persist=saved.append)

    assert saved == [], "a clause with no verdict must not be written to the table"


def test_persist_records_the_verdict_and_its_basis():
    saved = []

    _run(lambda *a, **k: _decision(0.9, 0.9, {"bgb-309-7-a": 0.9}), persist=saved.append)

    assert len(saved) == 1
    assert saved[0].clause_id == "agb-349"
    assert saved[0].legal_basis == ["bgb-309-7-a"]


def test_build_questions_keys_every_candidate_and_asks_about_voidness():
    questions = build_questions([_chunk("bgb-309-7-a"), _chunk("bgb-307-abs1", 2)])

    assert sorted(questions) == ["bgb-307-abs1", "bgb-309-7-a", "is_void"]
    assert all(q["type"] == "noul" for q in questions.values())
    # The chunk's own text is what the question is asked against.
    assert "Gesetzestext bgb-309-7-a" in questions["bgb-309-7-a"]["instructions"]
    assert "true_description" in questions["is_void"]["criteria"]


def test_build_questions_rejects_a_chunk_id_that_is_not_a_valid_question_key():
    bad = Retrieved(chunk_id="bgb 309/7", citation="x", text="y", score=1.0, rank=1)

    with pytest.raises(ValueError, match="not a valid question key"):
        build_questions([bad])


def test_read_answers_ignores_candidates_the_api_did_not_answer():
    chunks = [_chunk("bgb-309-7-a"), _chunk("bgb-307-abs1", 2)]
    partial = _decision(0.9, 0.9, {"bgb-309-7-a": 0.8})

    draft = read_answers(partial, chunks, void_threshold=0.5, basis_threshold=0.5)

    assert draft["legal_basis"] == ["bgb-309-7-a"]
    assert "bgb-307-abs1" not in draft["basis_scores"]

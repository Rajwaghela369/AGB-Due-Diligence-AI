"""Phase 7 interrupts (7.B): an uncertain clause waits for the lawyer and resumes on a decision.

The judge here is the real `make_judge` graph with a provider-shaped fake client, a fake
retriever and router, so nothing reaches Gemini, Jina or Qdrant. The fake client answers the
judge by clause text and records every explain call, which is how the tests see that a
resumed graph explains on the lawyer's §. A "restart" is a new judge and a new service
built on the same checkpointer and store. The Postgres variant runs only when
`TEST_DATABASE_URL` is set.
"""

import os
import queue
import threading
from types import SimpleNamespace

import pytest
from core.budget import SharedLimiter
from core.hashing import content_hash
from core.retrieval import Retrieved

from app.agent import prompts, run, task
from app.agent.graph import build_graph, find_cited, widened
from app.domain import ClauseSpan, ClauseVerdict, Decision, Document, Review
from app.review.checkpoint import memory_checkpointer
from app.review.runner import review_clauses, settled_status, thread_id
from app.review.service import build_service
from app.store.memory import memory_store

DEADLINE = 10.0
DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

MODEL_NORM = Retrieved("bgb-307-1", "§ 307 Abs. 1 BGB", "Unangemessene Benachteiligung.", 1.0, 1)
LAWYER_NORM = Retrieved("bgb-309-7-a", "§ 309 Nr. 7 a BGB", "Haftung für Leben und Körper.", 0.9, 2)
HIDDEN_NORM = Retrieved("bgb-309-8-b", "§ 309 Nr. 8 b BGB", "Gewährleistung.", 0.5, 9)

UNSURE = "Die Haftung für Schäden ist in jedem Fall begrenzt."
CLEAR = "Der Vertrag beginnt mit der Bestellung."
PRICES = {"input": 1.0, "output": 10.0}


# --- fakes ------------------------------------------------------------------------------


class Provider:
    """Gemini-shaped. The judge is unsure about UNSURE and sure that CLEAR is valid."""

    def __init__(self, reason: str = "Der Ausschluss verstößt gegen § 309 Nr. 7 a BGB.") -> None:
        self.reason = reason
        self.judged: list[str] = []
        self.explained: list[str] = []  # the user turn of every explain call
        self._lock = threading.Lock()
        self.decisions = SimpleNamespace(create=self.create)
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def create(self, *, model, input, questions):
        with self._lock:
            self.judged.append(input)
        unsure = input == UNSURE
        answers = {
            "is_void": SimpleNamespace(
                noul=0.55 if unsure else 0.1, confidence=0.4 if unsure else 0.95, probabilities=None
            )
        }
        for key in questions:
            if key != "is_void":
                answers[key] = SimpleNamespace(
                    noul=0.7 if key == MODEL_NORM.chunk_id else 0.1,
                    confidence=0.9,
                    probabilities=None,
                )
        return SimpleNamespace(
            answers=answers, usage=SimpleNamespace(total_input_tokens=1000, total_output_tokens=0)
        )

    def generate_content(self, **kwargs):
        with self._lock:
            self.explained.append(str(kwargs.get("contents")))
        return SimpleNamespace(
            text=f'{{"reason_de": "{self.reason}"}}',
            usage_metadata=SimpleNamespace(prompt_token_count=200, candidates_token_count=50),
        )


class Retriever:
    """Returns the model's and the lawyer's norm; HIDDEN_NORM only on the retry's wider net."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, text, topic, policy, *, depth):
        self.calls.append({"topic": topic, "policy": policy, "depth": depth})
        found = [MODEL_NORM, LAWYER_NORM]
        if policy.get("always_include_paragraphs"):
            found.append(HIDDEN_NORM)
        return found


def _judge(monkeypatch, provider, saver, retriever=None):
    prompts.clear_cache()
    monkeypatch.setattr(task, "index_hash", lambda: "index")
    config = {
        "models": {"judge": {"provider": "gemini", "name": "judge"}},
        "prompts": {"label": "evaluation"},
        "retrieval": {"top_k": 2, "search_depth": 4},
        "agent": {"explain_enabled": True},
    }
    return run.make_judge(
        config,
        limiter=SharedLimiter(),
        client=provider,
        router=lambda text: "liability",
        retriever=retriever or Retriever(),
        checkpointer=saver,
    )


class Recording:
    """Wraps a judge and records the thread ids it is resumed on."""

    def __init__(self, judge) -> None:
        self._judge = judge
        self.agent_version = judge.agent_version
        self.interrupts = judge.interrupts
        self.explain_only = judge.explain_only
        self.resumed: list[str] = []

    def __call__(self, clause, **kwargs):
        return self._judge(clause, **kwargs)

    def resume(self, clause, *, thread_id, verdict, basis):
        self.resumed.append(thread_id)
        return self._judge.resume(clause, thread_id=thread_id, verdict=verdict, basis=basis)


def _document(store, texts) -> str:
    document_id = content_hash("\n".join(texts))
    clauses = [
        ClauseSpan(position=i, number=f"{i + 1}.", text=t, content_hash=content_hash(t))
        for i, t in enumerate(texts)
    ]
    store.documents.save(
        Document(
            document_id=document_id,
            filename="agb.pdf",
            source_type="text",
            parser_version="p",
            splitter_version="s",
            text_hash=document_id,
        ),
        clauses,
        {c.content_hash: c.text for c in clauses},
    )
    return document_id


def _service(store, judge):
    return build_service(
        store, judge, SharedLimiter(), max_concurrency=1, poll_seconds=0.01, prices=PRICES
    )


def _collect(service, review_id: str) -> list:
    out: queue.Queue = queue.Queue()

    def read() -> None:
        try:
            for event in service.events(review_id):
                out.put(event)
        finally:
            out.put(None)

    threading.Thread(target=read, daemon=True).start()
    events = []
    while (event := out.get(timeout=DEADLINE)) is not None:
        events.append(event)
    return events


def _kinds(events) -> list[str]:
    def kind(event) -> str:
        if event.type == "STATE_DELTA":
            statuses = [
                op["value"]["status"]
                for op in event.payload["delta"]
                if op["path"].startswith("/clauses/")
            ]
            return "DELTA " + ",".join(statuses)
        if event.type in ("STEP_STARTED", "STEP_FINISHED"):
            return f"{event.type} {event.payload['stepName']}"
        if event.type == "CUSTOM":
            return f"CUSTOM {event.payload['name']}"
        return event.type

    return [kind(e) for e in events]


def _override(review_id: str, position: int, basis: list[str]) -> Decision:
    return Decision(
        review_id=review_id,
        position=position,
        action="override",
        corrected_verdict="potentially_void",
        corrected_basis=basis,
        model_verdict="uncertain",
        model_basis=[MODEL_NORM.chunk_id],
    )


def _checkpoint(saver, thread: str):
    return saver.get_tuple({"configurable": {"thread_id": thread}})


def _review_one(store, judge, texts=(UNSURE, CLEAR)):
    service = _service(store, judge)
    review = service.start(_document(store, list(texts)), ["liability"])
    events = _collect(service, review.review_id)
    assert service.wait(review.review_id, DEADLINE)
    return service, service.get(review.review_id), events


# --- the brief's tests -------------------------------------------------------------------


def _interrupt_then_restart_and_resume(monkeypatch, first_saver, second_saver):
    """The shared body of the memory and Postgres restart tests."""
    store = memory_store()
    provider = Provider()
    service, review, events = _review_one(store, _judge(monkeypatch, provider, first_saver))

    # The uncertain clause waits; the valid one is done; the review waits for decisions.
    assert review.status == "awaiting_decisions"
    assert review.counts.awaiting_decision == 1 and review.counts.done == 1
    statuses = {f.position: f.status for f in service.findings(review.review_id)}
    assert statuses == {0: "awaiting_decision", 1: "done"}
    assert _kinds(events) == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "STEP_STARTED clause:0",
        "DELTA checking",
        "DELTA awaiting_decision",
        "CUSTOM interrupt",
        "STEP_FINISHED clause:0",
        "CUSTOM progress",
        "STEP_STARTED clause:1",
        "DELTA checking",
        "DELTA done",
        "STEP_FINISHED clause:1",
        "CUSTOM progress",
        "RUN_FINISHED",
    ]
    interrupt = next(e for e in events if e.type == "CUSTOM" and e.payload["name"] == "interrupt")
    assert interrupt.payload["value"]["position"] == 0
    assert interrupt.payload["value"]["verdict"] == "uncertain"
    assert interrupt.payload["value"]["confidence"] == pytest.approx(0.4)
    assert interrupt.payload["value"]["question"]
    assert events[-1].payload["result"]["counts"]["awaiting_decision"] == 1
    # Only the waiting clause keeps a checkpoint; the finished one was deleted.
    assert _checkpoint(first_saver, thread_id(review.review_id, 0)) is not None
    assert _checkpoint(first_saver, thread_id(review.review_id, 1)) is None
    assert provider.judged == [UNSURE, CLEAR]
    assert provider.explained == []  # uncertain is never explained by the model

    service.shutdown(timeout=DEADLINE)

    # Restart: a new judge and service on the same checkpoints and store.
    restarted = Provider()
    judge = Recording(_judge(monkeypatch, restarted, second_saver))
    service = _service(store, judge)
    clause = review_clauses(store, review)[0]
    verdict = store.verdicts.get(clause.content_hash, review.agent_version)
    before = verdict.model_dump()
    seq_before = len(list(store.events.since(review.review_id, 0)))

    decision, cost = service._resume_decided(
        review, clause, verdict, _override(review.review_id, 0, [LAWYER_NORM.chunk_id])
    )

    assert judge.resumed == [f"{review.review_id}:0"]
    assert restarted.judged == []  # resumed, not judged again
    assert len(restarted.explained) == 1
    assert LAWYER_NORM.citation in restarted.explained[0]
    assert MODEL_NORM.citation not in restarted.explained[0]
    assert decision.reason_de == "Der Ausschluss verstößt gegen § 309 Nr. 7 a BGB."
    assert decision.reason_unavailable is False
    assert cost == pytest.approx((200 * 1.0 + 50 * 10.0) / 1e6)
    # The verdict row stays the model's; no status change and no events from the seam.
    assert store.verdicts.get(clause.content_hash, review.agent_version).model_dump() == before
    assert store.reviews.clause_statuses(review.review_id)[0][0] == "awaiting_decision"
    assert len(list(store.events.since(review.review_id, 0))) == seq_before
    # The thread is closed: a second resume finds nothing to continue.
    assert _checkpoint(second_saver, thread_id(review.review_id, 0)) is None
    service.shutdown(timeout=DEADLINE)


def test_uncertain_interrupts_and_a_restarted_service_resumes_the_checkpoint(monkeypatch):
    saver = memory_checkpointer().saver
    _interrupt_then_restart_and_resume(monkeypatch, saver, saver)


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_uncertain_interrupts_and_resumes_from_postgres_after_a_restart(monkeypatch):
    from app.review.checkpoint import postgres_checkpointer

    first = postgres_checkpointer(DATABASE_URL, max_size=2)
    second = None
    try:
        second = postgres_checkpointer(DATABASE_URL, max_size=2)  # a new pool: a new process
        _interrupt_then_restart_and_resume(monkeypatch, first.saver, second.saver)
    finally:
        first.close()
        if second is not None:
            second.close()


def test_a_cached_uncertain_verdict_waits_without_a_judge_call_and_explains_only(monkeypatch):
    store = memory_store()
    provider = Provider()
    saver = memory_checkpointer().saver
    judge = Recording(_judge(monkeypatch, provider, saver))
    first_service, first, _ = _review_one(store, judge)
    assert first.status == "awaiting_decisions"
    first_service.shutdown(timeout=DEADLINE)

    # The same document again: every verdict is cached, nothing is judged.
    provider.judged.clear()
    service, review, events = _review_one(store, judge)
    assert provider.judged == []
    assert review.status == "awaiting_decisions"
    assert review.counts.cache_hits == 2 and review.counts.awaiting_decision == 1
    assert _kinds(events) == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "DELTA awaiting_decision,done",
        "CUSTOM interrupt",
        "CUSTOM progress",
        "RUN_FINISHED",
    ]
    assert _checkpoint(saver, thread_id(review.review_id, 0)) is None  # no graph for a hit

    clause = review_clauses(store, review)[0]
    verdict = store.verdicts.get(clause.content_hash, review.agent_version)

    # A non-void decision on a cached verdict makes no call at all.
    accept = Decision(
        review_id=review.review_id,
        position=0,
        action="accept",
        model_verdict="uncertain",
        model_basis=list(verdict.legal_basis),
    )
    kept, cost = service._resume_decided(review, clause, verdict, accept)
    assert (kept, cost) == (accept, 0.0)
    assert provider.explained == []

    # A void override explains only: one explain call on the lawyer's §, no judge call.
    decision, cost = service._resume_decided(
        review, clause, verdict, _override(review.review_id, 0, [LAWYER_NORM.chunk_id])
    )
    assert judge.resumed == [thread_id(review.review_id, 0)] * 2  # tried, nothing waited
    assert provider.judged == []
    assert len(provider.explained) == 1 and LAWYER_NORM.citation in provider.explained[0]
    assert decision.reason_de and decision.reason_unavailable is False
    assert cost == pytest.approx((200 * 1.0 + 50 * 10.0) / 1e6)
    assert store.verdicts.get(clause.content_hash, review.agent_version) == verdict
    service.shutdown(timeout=DEADLINE)


# --- resume edge cases -----------------------------------------------------------------


def _waiting(monkeypatch, provider=None, retriever=None):
    """A service whose review has clause 0 interrupted, plus what the tests need."""
    store = memory_store()
    provider = provider or Provider()
    saver = memory_checkpointer().saver
    judge = _judge(monkeypatch, provider, saver, retriever)
    service, review, _ = _review_one(store, judge, texts=(UNSURE,))
    clause = review_clauses(store, review)[0]
    verdict = store.verdicts.get(clause.content_hash, review.agent_version)
    return SimpleNamespace(
        store=store,
        provider=provider,
        saver=saver,
        service=service,
        review=review,
        clause=clause,
        verdict=verdict,
    )


@pytest.mark.parametrize("action", ["accept", "dismiss"])
def test_a_non_void_decision_closes_the_checkpoint_without_a_model_call(monkeypatch, action):
    w = _waiting(monkeypatch)
    decision = Decision(
        review_id=w.review.review_id,
        position=0,
        action=action,
        model_verdict="uncertain",
        model_basis=list(w.verdict.legal_basis),
    )
    result, cost = w.service._resume_decided(w.review, w.clause, w.verdict, decision)
    assert (result, cost) == (decision, 0.0)
    assert w.provider.explained == [] and w.provider.judged == [UNSURE]
    assert _checkpoint(w.saver, thread_id(w.review.review_id, 0)) is None
    w.service.shutdown(timeout=DEADLINE)


def test_a_lawyer_basis_the_judge_never_saw_is_found_with_the_retry_search(monkeypatch):
    retriever = Retriever()
    w = _waiting(monkeypatch, retriever=retriever)
    retriever.calls.clear()
    decision, _ = w.service._resume_decided(
        w.review, w.clause, w.verdict, _override(w.review.review_id, 0, [HIDDEN_NORM.chunk_id])
    )
    assert decision.reason_unavailable is False
    assert HIDDEN_NORM.citation in w.provider.explained[0]
    # Topic search first, then the retry's wider net, which is where the § was.
    assert [call["topic"] for call in retriever.calls] == ["liability", ""]
    assert retriever.calls[1]["policy"]["always_include_paragraphs"]
    w.service.shutdown(timeout=DEADLINE)


def test_a_failed_explanation_on_resume_is_reason_unavailable_not_an_error(monkeypatch):
    provider = Provider()
    provider.generate_content = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("503"))
    provider.models = SimpleNamespace(generate_content=provider.generate_content)
    monkeypatch.setattr("core.llm.time.sleep", lambda _s: None)
    w = _waiting(monkeypatch, provider=provider)
    decision, cost = w.service._resume_decided(
        w.review, w.clause, w.verdict, _override(w.review.review_id, 0, [LAWYER_NORM.chunk_id])
    )
    assert decision.reason_unavailable is True and decision.reason_de == ""
    assert cost == 0.0
    w.service.shutdown(timeout=DEADLINE)


def test_a_broken_checkpoint_store_never_raises_from_resume(monkeypatch):
    w = _waiting(monkeypatch)

    def broken(*args, **kwargs):
        raise ConnectionError("checkpoint store down")

    monkeypatch.setattr(w.saver, "get_tuple", broken)
    decision, cost = w.service._resume_decided(
        w.review, w.clause, w.verdict, _override(w.review.review_id, 0, [LAWYER_NORM.chunk_id])
    )
    assert decision.reason_unavailable is True and cost == 0.0
    assert w.provider.explained == []  # not retried as explain-only: one decision, one try
    w.service.shutdown(timeout=DEADLINE)


def test_a_judge_without_resume_explains_only():
    """The fake judges of the runner tests have no `resume`: the seam still works."""

    class ExplainOnly:
        agent_version = "v"

        def __init__(self) -> None:
            self.calls = 0

        def explain_only(self, clause, verdict):
            self.calls += 1
            assert verdict.verdict == "potentially_void"
            assert verdict.legal_basis == [LAWYER_NORM.chunk_id]
            return verdict.model_copy(
                update={"reason_de": "Begründung.", "input_tokens": verdict.input_tokens + 10}
            )

    judge = ExplainOnly()
    service = build_service(memory_store(), judge, SharedLimiter(), prices=PRICES)
    clause = ClauseSpan(position=0, text=UNSURE, content_hash=content_hash(UNSURE))
    verdict = ClauseVerdict(
        content_hash=clause.content_hash, agent_version="v", verdict="uncertain", confidence=0.4
    )
    review = Review(
        review_id="r", document_id="d", agent_version="v", scope=["liability"], status="finished"
    )
    decision, cost = service._resume_decided(
        review, clause, verdict, _override("r", 0, [LAWYER_NORM.chunk_id])
    )
    assert judge.calls == 1 and decision.reason_de == "Begründung."
    assert cost == pytest.approx(10 / 1e6)


# --- the graph and the runner helpers ------------------------------------------------------


def test_without_a_checkpointer_the_graph_never_interrupts(monkeypatch):
    provider = Provider()
    judge = _judge(monkeypatch, provider, saver=None)
    assert judge.interrupts is False
    clause = ClauseSpan(position=0, text=UNSURE, content_hash=content_hash(UNSURE))
    verdict = judge(clause)
    assert verdict.verdict == "uncertain"
    assert judge.resume(clause, thread_id="r:0", verdict="valid", basis=[]) is None


def test_the_runner_passes_the_thread_id_only_to_an_interrupting_judge():
    seen: list[dict] = []

    class Judge:
        agent_version = "v"
        interrupts = True

        def __call__(self, clause, **kwargs):
            seen.append(kwargs)
            return ClauseVerdict(
                content_hash=clause.content_hash, agent_version="v", verdict="valid", confidence=0.9
            )

    store = memory_store()
    service = _service(store, Judge())
    review = service.start(_document(store, [CLEAR]), ["liability"])
    assert service.wait(review.review_id, DEADLINE)
    assert seen[0]["thread_id"] == f"{review.review_id}:0"
    review = service.get(review.review_id)
    assert review.status == "finished" and review.counts.done == 1


def test_an_uncertain_verdict_already_decided_is_done():
    verdict = ClauseVerdict(content_hash="h", agent_version="v", verdict="uncertain", confidence=0)
    assert settled_status(verdict, decided=False) == "awaiting_decision"
    assert settled_status(verdict, decided=True) == "done"
    assert settled_status(verdict.model_copy(update={"verdict": "valid"}), False) == "done"


def test_find_cited_searches_only_for_what_is_missing():
    calls: list[str] = []

    def retriever(text, topic, policy, *, depth):
        calls.append(topic)
        return [MODEL_NORM, LAWYER_NORM, HIDDEN_NORM]

    policy = {"top_k": 2, "search_depth": 4}
    have = [MODEL_NORM]
    assert find_cited(retriever, "t", "x", policy, [MODEL_NORM.chunk_id], have=have) == have
    assert calls == []
    cited = find_cited(retriever, "t", "x", policy, [MODEL_NORM.chunk_id, HIDDEN_NORM.chunk_id])
    assert [c.chunk_id for c in cited] == [MODEL_NORM.chunk_id, HIDDEN_NORM.chunk_id]
    assert calls == ["x", ""]  # top_k=2 misses HIDDEN_NORM; the wider k=3 finds it
    wider, wider_k = widened(policy)
    assert wider["search_depth"] == 8 and wider_k == 3 and wider["always_include_paragraphs"]


def test_checkpoints_hold_retrieved_chunks_without_a_serde_warning(caplog):
    from core.llm import Answer, DecisionResult

    saver = memory_checkpointer().saver
    graph = build_graph(
        decider=lambda *a, **k: DecisionResult(
            {"is_void": Answer(0.5, 0.3, None), MODEL_NORM.chunk_id: Answer(0.9, 0.9, None)},
            10,
            0,
            "m",
        ),
        retriever=lambda text, topic, policy, depth=20: [MODEL_NORM],
        checkpointer=saver,
    )
    config = {"configurable": {"thread_id": "t"}}
    with caplog.at_level("WARNING"):
        state = graph.invoke(
            {"clause": {"clause_id": "c", "title": None, "text": "x"}, "retries": 0}, config
        )
        assert state["__interrupt__"][0].value["verdict"] == "uncertain"
        assert graph.get_state(config).values["candidates"] == [MODEL_NORM]
    assert "unregistered type" not in caplog.text


def test_a_repeated_uncertain_clause_waits_twice_but_only_the_judged_one_has_a_graph(monkeypatch):
    store = memory_store()
    provider = Provider()
    saver = memory_checkpointer().saver
    service, review, events = _review_one(
        store, _judge(monkeypatch, provider, saver), texts=(UNSURE, UNSURE)
    )
    assert provider.judged == [UNSURE]  # the second is a cache hit found during the run
    kinds = _kinds(events)
    later = kinds[kinds.index("STEP_FINISHED clause:0") + 2 :]
    assert later == [
        "DELTA awaiting_decision",
        "CUSTOM interrupt",
        "CUSTOM progress",
        "RUN_FINISHED",
    ]
    assert review.counts.awaiting_decision == 2 and review.counts.cache_hits == 1
    assert _checkpoint(saver, thread_id(review.review_id, 0)) is not None
    assert _checkpoint(saver, thread_id(review.review_id, 1)) is None

    clauses = review_clauses(store, review)
    verdict = store.verdicts.get(clauses[1].content_hash, review.agent_version)
    decision, _ = service._resume_decided(
        review, clauses[1], verdict, _override(review.review_id, 1, [LAWYER_NORM.chunk_id])
    )
    assert decision.reason_de  # explain only, since clause 1 never had a graph
    assert _checkpoint(saver, thread_id(review.review_id, 0)) is not None  # untouched
    service.shutdown(timeout=DEADLINE)

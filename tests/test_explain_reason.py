"""Regenerating a missing reasoning: the explain step alone, through the API, on fakes.

Nothing here reaches Gemini. `make_judge` gets a provider-shaped fake client, and the
API tests swap in a fake judge whose `explain_only` counts calls.
"""

import threading
import time
from types import SimpleNamespace

import pytest
from core.budget import SharedLimiter
from core.hashing import content_hash
from core.retrieval import Retrieved
from fastapi.testclient import TestClient

from app.agent import prompts, run, task
from app.agent.run import ExplainFailed
from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, Document, Review
from app.review.limiter import build_limiter
from app.review.runner import ReviewRun
from app.review.service import build_service
from app.store.memory import memory_store

CHUNK = Retrieved("bgb-309-7-a", "§ 309 Nr. 7 BGB", "Gesetzestext", 1.0, 1)
VOID_TEXT = "Unsere Haftung ist ausgeschlossen."
VERSION = "agent-explain"


# --- the agent's explain_only --------------------------------------------------------


class Provider:
    """A Gemini-shaped client whose text calls follow a script of replies or errors."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.models = SimpleNamespace(generate_content=self.generate_content)

    def generate_content(self, **kwargs):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            text=reply,
            usage_metadata=SimpleNamespace(prompt_token_count=7, candidates_token_count=3),
        )


class StatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


def _judge(provider: Provider, monkeypatch):
    prompts.clear_cache()
    monkeypatch.setattr(task, "index_hash", lambda: "index")
    monkeypatch.setattr("core.llm.time.sleep", lambda _s: None)
    config = {
        "models": {"judge": {"provider": "gemini", "name": "judge"}},
        "prompts": {"label": "evaluation"},
        "retrieval": {"top_k": 1, "search_depth": 1},
        "agent": {"explain_enabled": True},
    }
    return run.make_judge(
        config,
        limiter=SharedLimiter(),
        client=provider,
        router=lambda text: "",
        retriever=lambda *args, **kwargs: [CHUNK],
    )


def _void(text: str = VOID_TEXT) -> ClauseVerdict:
    return ClauseVerdict(
        content_hash=content_hash(text),
        agent_version=VERSION,
        verdict="potentially_void",
        legal_basis=[CHUNK.chunk_id],
        candidates=[CHUNK.chunk_id],
        confidence=0.9,
        reason_unavailable=True,
        input_tokens=100,
        output_tokens=10,
    )


def _span(text: str = VOID_TEXT) -> ClauseSpan:
    return ClauseSpan(
        position=0, number="1.", heading="Haftung", text=text, content_hash=content_hash(text)
    )


def test_explain_only_makes_one_call_and_fills_the_reason(monkeypatch):
    provider = Provider('{"reason_de": "Der Ausschluss verstößt gegen § 309 Nr. 7 BGB."}')
    explained = _judge(provider, monkeypatch).explain_only(_span(), _void())

    assert provider.calls == 1
    assert explained.reason_de == "Der Ausschluss verstößt gegen § 309 Nr. 7 BGB."
    assert explained.reason_unavailable is False
    assert (explained.input_tokens, explained.output_tokens) == (107, 13)
    assert explained.verdict == "potentially_void"


def test_explain_only_retries_a_429_through_the_shared_retry_path(monkeypatch):
    provider = Provider(StatusError(429), '{"reason_de": "Begründung."}')
    explained = _judge(provider, monkeypatch).explain_only(_span(), _void())

    assert provider.calls == 2
    assert explained.reason_de == "Begründung."


def test_explain_only_fails_cleanly_when_the_provider_keeps_failing(monkeypatch):
    provider = Provider(*[StatusError(503)] * 10)
    with pytest.raises(ExplainFailed):
        _judge(provider, monkeypatch).explain_only(_span(), _void())


def test_explain_only_fails_when_the_cited_norm_is_gone(monkeypatch):
    judge = _judge(Provider(), monkeypatch)
    verdict = _void().model_copy(update={"legal_basis": ["bgb-gone"]})
    with pytest.raises(ExplainFailed):
        judge.explain_only(_span(), verdict)


# --- the endpoint -------------------------------------------------------------------


class FakeJudge:
    agent_version = VERSION

    def __init__(self, store, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.explains = 0

    def __call__(self, clause, *, parent=None):
        if "ausgeschlossen" in clause.text:
            # The first judge call lost its reasoning, which is the bug being fixed.
            return _void(clause.text)
        return ClauseVerdict(
            content_hash=clause.content_hash,
            agent_version=VERSION,
            verdict="valid",
            confidence=0.9,
            reason_de="Alles gut.",
        )

    def explain_only(self, clause, verdict):
        self.explains += 1
        if self.fail:
            raise ExplainFailed("Gemini antwortet nicht.")
        return verdict.model_copy(update={"reason_de": "Neue Begründung.", "input_tokens": 120})


def _client(fail: bool = False):
    store = memory_store()
    judge = FakeJudge(store, fail)
    service = build_service(store, judge, build_limiter({}), max_concurrency=1, poll_seconds=0.01)
    app = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=None,
        settings={"ingest": {"max_upload_mb": 20}, "api": {"rate_limit_per_minute": 100000}},
    )
    return app, store, judge


def _review(client, store) -> tuple[str, dict[str, int]]:
    texts = [VOID_TEXT, "Die Lieferung erfolgt in zwei Wochen."]
    document_id = content_hash("\n".join(texts))
    store.documents.save(
        Document(
            document_id=document_id,
            filename="a.txt",
            source_type="text",
            parser_version="p",
            splitter_version="s",
            text_hash=document_id,
        ),
        [
            ClauseSpan(position=i, number=f"{i + 1}.", text=t, content_hash=content_hash(t))
            for i, t in enumerate(texts)
        ],
        {content_hash(t): t for t in texts},
    )
    review_id = client.post("/api/v1/reviews", json={"document_id": document_id}).json()["review"][
        "review_id"
    ]
    assert "RUN_FINISHED" in client.get(f"/api/v1/reviews/{review_id}/events").text
    return review_id, {"void": 0, "valid": 1}


def test_explain_endpoint_fills_the_reason_and_emits_a_delta():
    app, store, judge = _client()
    with TestClient(app) as client:
        review_id, pos = _review(client, store)
        response = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['void']}/explain")
        findings = client.get(f"/api/v1/reviews/{review_id}/findings").json()["findings"]
        events = client.get(f"/api/v1/reviews/{review_id}/events").text

    assert response.status_code == 200
    assert response.json()["verdict"]["reason_de"] == "Neue Begründung."
    assert response.json()["verdict"]["reason_unavailable"] is False
    assert findings[pos["void"]]["verdict"]["reason_de"] == "Neue Begründung."  # stored
    assert judge.explains == 1
    assert "Neue Begründung." in events  # the STATE_DELTA
    assert "event: RUN_FINISHED" in events


def test_explain_endpoint_is_409_for_a_valid_verdict_and_for_an_existing_reason():
    app, store, judge = _client()
    with TestClient(app) as client:
        review_id, pos = _review(client, store)
        valid = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['valid']}/explain")
        first = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['void']}/explain")
        again = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['void']}/explain")
        missing = client.post(f"/api/v1/reviews/{review_id}/clauses/99/explain")
        unknown = client.post("/api/v1/reviews/nope/clauses/0/explain")

    assert valid.status_code == 409
    assert first.status_code == 200
    assert again.status_code == 409
    assert missing.status_code == 404
    assert unknown.status_code == 404
    assert judge.explains == 1, "a refused request must not cost a Gemini call"


def test_explain_endpoint_reports_a_provider_failure_and_keeps_the_verdict():
    app, store, judge = _client(fail=True)
    with TestClient(app) as client:
        review_id, pos = _review(client, store)
        response = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['void']}/explain")
        finding = client.get(f"/api/v1/reviews/{review_id}/findings").json()["findings"][
            pos["void"]
        ]
        judge.fail = False
        retry = client.post(f"/api/v1/reviews/{review_id}/clauses/{pos['void']}/explain")

    assert response.status_code == 502
    assert response.json()["code"] == "explain_failed"
    assert finding["verdict"]["reason_unavailable"] is True
    assert retry.status_code == 200


# --- terminal events and run-local caches -------------------------------------------


class GatedJudge(FakeJudge):
    """Holds a slow clause open, and holds `explain_only` open, until the test lets go."""

    def __init__(self) -> None:
        super().__init__(None)
        self.slow = threading.Event()
        self.explain_entered = threading.Event()
        self.explain_go = threading.Event()

    def __call__(self, clause, *, parent=None):
        if "langsam" in clause.text:
            assert self.slow.wait(10)
        return super().__call__(clause, parent=parent)

    def explain_only(self, clause, verdict):
        self.explain_entered.set()
        assert self.explain_go.wait(10)
        return super().explain_only(clause, verdict)


def _save_document(store, texts: list[str]) -> str:
    document_id = content_hash("\n".join(texts))
    store.documents.save(
        Document(
            document_id=document_id,
            filename="a.txt",
            source_type="text",
            parser_version="p",
            splitter_version="s",
            text_hash=document_id,
        ),
        [
            ClauseSpan(position=i, number=f"{i + 1}.", text=t, content_hash=content_hash(t))
            for i, t in enumerate(texts)
        ],
        {content_hash(t): t for t in texts},
    )
    return document_id


def _wait(predicate, what: str) -> None:
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


def test_a_review_finishing_during_explain_keeps_every_terminal_last():
    store = memory_store()
    judge = GatedJudge()
    service = build_service(store, judge, build_limiter({}), max_concurrency=2, poll_seconds=0.01)
    document_id = _save_document(store, [VOID_TEXT, "Eine langsame Klausel."])
    review_id = service.start(document_id, []).review_id
    _wait(
        lambda: store.reviews.clause_statuses(review_id).get(0, ("",))[0] == "done",
        "void clause judged",
    )

    result: list = []
    caller = threading.Thread(target=lambda: result.append(service.explain_clause(review_id, 0)))
    caller.start()
    assert judge.explain_entered.wait(10)
    judge.slow.set()  # the run finishes while the paid call is in flight
    assert service.wait(review_id, 10)
    judge.explain_go.set()
    caller.join(10)
    assert result[0].verdict.reason_de == "Neue Begründung."

    events = list(service.events(review_id, 0, follow=False))
    types = [e.type for e in events]
    # (c) nothing but a new RUN_STARTED ever follows a terminal event.
    for i, kind in enumerate(types[:-1]):
        if kind in ("RUN_FINISHED", "RUN_ERROR"):
            assert types[i + 1] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    # (a) a full replay contains the regenerated delta and ends on a terminal.
    assert types[-4:] == ["RUN_STARTED", "STATE_SNAPSHOT", "STATE_DELTA", "RUN_FINISHED"]
    assert "Neue Begründung." in str(events[-2].payload)
    assert "Neue Begründung." in str(events[-3].payload)
    # (b) a client that stopped at the original terminal sees the new events and stops.
    first_terminal = types.index("RUN_FINISHED")
    tail = list(service.events(review_id, events[first_terminal].seq))
    # The reconnect's own seq-0 snapshot comes first, then exactly the new events.
    assert [e.type for e in tail][1:] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "STATE_DELTA",
        "RUN_FINISHED",
    ]
    assert "Neue Begründung." in str([e.payload for e in tail])


def test_a_running_review_gets_only_a_delta_before_its_own_terminal():
    store = memory_store()
    judge = GatedJudge()
    judge.explain_go.set()
    service = build_service(store, judge, build_limiter({}), max_concurrency=2, poll_seconds=0.01)
    document_id = _save_document(store, [VOID_TEXT, "Eine langsame Klausel."])
    review_id = service.start(document_id, []).review_id
    _wait(
        lambda: store.reviews.clause_statuses(review_id).get(0, ("",))[0] == "done",
        "void clause judged",
    )
    service.explain_clause(review_id, 0)
    judge.slow.set()
    assert service.wait(review_id, 10)

    types = [e.type for e in service.events(review_id, 0, follow=False)]
    assert types.count("RUN_FINISHED") == 1 and types[-1] == "RUN_FINISHED"
    assert types.count("RUN_STARTED") == 1


def test_a_concurrent_review_holding_the_old_verdict_emits_the_new_reason():
    store = memory_store()
    judge = GatedJudge()
    judge.explain_go.set()
    service = build_service(store, judge, build_limiter({}), max_concurrency=1, poll_seconds=0.01)
    judge.slow.set()
    first = service.start(_save_document(store, [VOID_TEXT]), []).review_id
    assert service.wait(first, 10)

    other = Review(
        review_id="other",
        document_id=_save_document(store, [VOID_TEXT, "Noch eine Klausel."]),
        agent_version=VERSION,
        scope=[],
        status="queued",
    )
    store.reviews.create(other)
    clauses = store.documents.clauses(other.document_id)
    run = ReviewRun(
        service._ctx,
        other,
        clauses,
        positions=None,
        reason="start",
        finish_gate=service._lock,
        on_closed=lambda _run: None,
    )
    run.open()  # reads the verdict now, while its reasoning is still missing
    run.announce()

    with service._lock:
        service._runs["other"] = run  # as if the other review were still going
    service.explain_clause(first, 0)
    with service._lock:
        del service._runs["other"]
    run._resolve_cached()

    events = list(service.events("other", 0, follow=False))
    deltas = [str(e.payload) for e in events if e.type == "STATE_DELTA"]
    assert deltas and all("Neue Begründung." in d for d in deltas)
    assert not any("reason_unavailable': True" in d for d in deltas)

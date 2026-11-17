"""The HTTP contract is exercised with memory stores and fake external services only."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from core.hashing import content_hash
from core.paths import ROOT
from core.schemas import LawChunk
from fastapi.testclient import TestClient

from app.api.main import build_app, create_app
from app.api.rate_limit import IpRateLimiter
from app.api.sse import stream_events
from app.domain import (
    ClauseSpan,
    ClauseVerdict,
    Document,
    Finding,
    Review,
    StepObservations,
    StoredEvent,
)
from app.ingest import IngestError
from app.review.deliverable import deliverable_settings
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.retention import RetentionSettings
from app.review.service import build_service
from app.store.memory import memory_store


def _pdf(text: str = "§ 1 Haftung gilt.") -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(payload)


class FakeLangfuse:
    def __init__(self) -> None:
        self.scores: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None
        self.api = SimpleNamespace(scores=SimpleNamespace(delete=self._delete_score))

    def create_score(self, **kwargs) -> None:
        self.scores[kwargs["score_id"]] = kwargs
        self.calls.append(kwargs)

    def _delete_score(self, score_id: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(score_id)
        self.scores.pop(score_id, None)


class FakeReviewService:
    def __init__(self, store) -> None:
        self.store = store
        self.resumed = 0
        self.closed = False
        self.retry_error: Exception | None = None
        self.start_error: Exception | None = None
        self.event_log: list[StoredEvent] = []
        self.feedback = None

    def start(self, document_id, scope) -> Review:
        if self.start_error:
            raise self.start_error
        review = Review(
            review_id="review-1",
            document_id=document_id,
            agent_version="agent-1",
            scope=scope,
            status="running",
        )
        return self.store.reviews.create(review)

    def get(self, review_id: str) -> Review | None:
        return self.store.reviews.get(review_id)

    def findings(self, review_id: str) -> list[Finding]:
        review = self.store.reviews.get(review_id)
        if review is None:
            return []
        return [
            Finding(position=clause.position, clause=clause, status="queued")
            for clause in self.store.documents.clauses(review.document_id)
        ]

    def retry_clause(self, review_id: str, position: int) -> None:
        if self.retry_error:
            raise self.retry_error

    def decide(self, review_id, position, decision, reviewer, *, request_id=None) -> Finding:
        stored = self.feedback.decide(
            review_id,
            position,
            decision,
            decided_by=reviewer.name,
            request_id=request_id,
        )
        review = self.store.reviews.get(review_id)
        clause = self.store.documents.clauses(review.document_id)[position]
        verdict = self.store.verdicts.get(clause.content_hash, review.agent_version)
        return Finding(
            position=position,
            clause=clause,
            status="done",
            verdict=verdict,
            decision=stored,
        )

    def events(
        self, review_id: str, after_seq: int = 0, *, follow: bool = True
    ) -> Iterator[StoredEvent]:
        if after_seq:
            yield StoredEvent(
                review_id=review_id,
                seq=0,
                type="STATE_SNAPSHOT",
                payload={"snapshot": {"review": review_id}},
            )
        yield from (event for event in self.event_log if event.seq > after_seq)

    def resume_running(self) -> int:
        self.resumed += 1
        return 0

    def shutdown(self, timeout: float | None = None) -> None:
        self.closed = True


def _statute() -> LawChunk:
    return LawChunk(
        chunk_id="bgb-309-7",
        law="BGB",
        paragraph="§ 309",
        item="7",
        title="Haftung",
        citation="§ 309 Nr. 7",
        topics=["liability"],
        embed_text="Haftung",
        text="Eine Bestimmung ist unwirksam.",
        source_dump="test",
    )


@pytest.fixture
def api():
    store = memory_store()
    service = FakeReviewService(store)
    langfuse = FakeLangfuse()
    feedback = FeedbackService(store, langfuse)
    service.feedback = feedback
    application = create_app(
        store=store,
        service=service,
        statutes={"bgb-309-7": _statute()},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}},
    )
    return application, store, service, langfuse


def _save_document(store, *, document_id: str = "doc-1") -> ClauseSpan:
    clause = ClauseSpan(
        position=0,
        number="§ 1",
        text="Haftung",
        content_hash=content_hash("Haftung"),
    )
    store.documents.save(
        Document(
            document_id=document_id,
            filename="terms.pdf",
            source_type="pdf",
            page_count=1,
            parser_version="p1",
            splitter_version="s1",
            text_hash=content_hash("Haftung"),
        ),
        [clause],
        {clause.content_hash: clause.text},
    )
    return clause


def _save_verdict(store, clause: ClauseSpan, *, review_id: str = "review-1") -> Review:
    review = store.reviews.create(
        Review(
            review_id=review_id,
            document_id="doc-1",
            agent_version="agent-1",
            scope=["liability"],
            status="finished",
        )
    )
    store.verdicts.save(
        ClauseVerdict(
            content_hash=clause.content_hash,
            agent_version="agent-1",
            verdict="potentially_void",
            confidence=0.8,
            trace_id="trace-1",
            observations=StepObservations(
                clause_span="span-1",
                judge="judge-1",
                retrieval="retrieve-1",
                explanation="explain-1",
            ),
        )
    )
    return review


def test_upload_reupload_paste_and_file_cache_headers(api):
    application, _store, _service, _langfuse = api
    with TestClient(application) as client:
        first = client.post("/api/v1/documents", files={"file": ("terms.pdf", _pdf())})
        second = client.post("/api/v1/documents", files={"file": ("again.pdf", _pdf())})
        paste = client.post(
            "/api/v1/documents",
            json={"text": "§ 1 Haftung gilt.\n§ 2 Gewährleistung gilt.", "filename": "paste"},
        )
        file_response = client.get(
            f"/api/v1/documents/{first.json()['document']['document_id']}/file"
        )
        paste_file = client.get(f"/api/v1/documents/{paste.json()['document']['document_id']}/file")
        missing_file = client.get("/api/v1/documents/nope/file")

    assert first.status_code == 200
    assert first.json()["clauses"]
    assert second.json()["reused"] is True
    assert all(clause["start"] is not None for clause in paste.json()["clauses"])
    assert file_response.content.startswith(b"%PDF-")
    assert file_response.headers["cache-control"].endswith("immutable")
    assert file_response.headers["etag"].startswith('"')
    assert paste_file.status_code == 200
    assert paste_file.headers["content-type"] == "text/plain; charset=utf-8"
    assert paste_file.content.decode() == "§ 1 Haftung gilt.\n§ 2 Gewährleistung gilt."
    assert missing_file.status_code == 404


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("too_large", 413),
        ("unsupported_type", 415),
        ("encrypted", 422),
        ("no_text_layer", 422),
        ("parse_failed", 422),
        ("empty", 422),
    ],
)
def test_ingest_errors_have_stable_bodies(api, monkeypatch, code, status):
    application, _store, _service, _langfuse = api

    def fail(*args, **kwargs):
        raise IngestError(code, "kaputt")

    monkeypatch.setattr("app.api.routes.documents.ingest_file", fail)
    with TestClient(application) as client:
        response = client.post("/api/v1/documents", files={"file": ("terms.pdf", _pdf())})
    assert response.status_code == status
    assert response.json() == {"code": code, "message": "kaputt"}


def test_request_size_and_per_ip_rate_limits_use_stable_errors(api):
    application, _store, _service, _langfuse = api
    application.state.settings = {
        "ingest": {"max_upload_mb": 1},
        "api": {"rate_limit_per_minute": 1, "rate_limit_burst": 1},
    }
    # create_app has already built the limiter, so replace it with the injected limits.
    application.state.rate_limiter = IpRateLimiter(capacity=1, per_minute=1)
    with TestClient(application) as client:
        too_large = client.post(
            "/api/v1/documents",
            content=b"x" * (1024 * 1024 + 1),
            headers={"content-type": "application/pdf"},
        )
        limited = client.post("/api/v1/reviews", json={"document_id": "missing"})

    assert (too_large.status_code, too_large.json()["code"]) == (413, "too_large")
    assert (limited.status_code, limited.json()["code"]) == (429, "rate_limited")


def test_review_findings_retry_statutes_health_and_readiness(api):
    application, store, service, _langfuse = api
    _save_document(store)
    with TestClient(application) as client:
        started = client.post("/api/v1/reviews", json={"document_id": "doc-1"})
        findings = client.get("/api/v1/reviews/review-1/findings")
        missing = client.get("/api/v1/reviews/missing/findings")
        service.retry_error = ValueError("not failed")
        conflict = client.post("/api/v1/reviews/review-1/clauses/0/retry")
        service.retry_error = KeyError("position")
        missing_clause = client.post("/api/v1/reviews/review-1/clauses/99/retry")
        statute = client.get("/api/v1/statutes/bgb-309-7")
        no_statute = client.get("/api/v1/statutes/missing")
        health = client.get("/api/v1/health")
        ready = client.get("/api/v1/ready")

    assert started.status_code == 200
    assert len(findings.json()["findings"]) == 1
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert (conflict.status_code, conflict.json()["code"]) == (409, "conflict")
    assert missing_clause.status_code == 404
    assert statute.json()["citation"] == "§ 309 Nr. 7"
    assert no_statute.status_code == 404
    assert health.json() == {"status": "ok"}
    assert ready.json() == {"ready": True, "checks": {}}


def test_sse_full_stream_and_reconnect_snapshot_without_id(api):
    application, store, service, _langfuse = api
    _save_document(store)
    _save_verdict(store, store.documents.clauses("doc-1")[0])
    service.event_log = [
        StoredEvent(review_id="review-1", seq=1, type="RUN_STARTED", payload={"runId": "r"}),
        StoredEvent(review_id="review-1", seq=2, type="STATE_SNAPSHOT", payload={"snapshot": {}}),
        StoredEvent(review_id="review-1", seq=3, type="STATE_DELTA", payload={"delta": []}),
        StoredEvent(review_id="review-1", seq=4, type="RUN_FINISHED", payload={"result": {}}),
    ]
    with TestClient(application) as client:
        full = client.get("/api/v1/reviews/review-1/events")
        resumed = client.get("/api/v1/reviews/review-1/events", headers={"Last-Event-ID": "2"})

    assert full.text.count("id: ") == 4
    assert "event: RUN_FINISHED" in full.text
    messages = [message for message in resumed.text.split("\n\n") if message]
    assert messages[0].startswith("event: STATE_SNAPSHOT\n")
    assert "id:" not in messages[0]
    assert [line for line in resumed.text.splitlines() if line.startswith("id:")] == [
        "id: 3",
        "id: 4",
    ]


def test_feedback_scores_steps_stably_and_clause_cut_is_store_only(api):
    application, store, _service, langfuse = api
    clause = _save_document(store)
    _save_verdict(store, clause)
    payload = {
        "review_id": "review-1",
        "position": 0,
        "vote": "down",
        "reasons": ["verdict_wrong", "basis_wrong", "reasoning_unclear", "clause_cut"],
        "corrected_verdict": "valid",
        "corrected_basis": ["bgb-309-7"],
        "comment": "Bitte korrigieren",
    }
    with TestClient(application) as client:
        first = client.post("/api/v1/feedback", json=payload)
        payload["comment"] = "Geänderte Bewertung"
        second = client.post("/api/v1/feedback", json=payload)

    assert first.json()["scored_steps"] == ["judge", "retrieval", "explanation"]
    assert first.json()["feedback_id"] == second.json()["feedback_id"]
    assert len(langfuse.scores) == 3
    assert {score["observation_id"] for score in langfuse.scores.values()} == {
        "judge-1",
        "retrieve-1",
        "explain-1",
    }
    assert all(
        "Korrigiertes Urteil: valid" in score["comment"] for score in langfuse.scores.values()
    )
    first_score_ids = set(langfuse.scores)
    stored = store.feedback.for_review("review-1")[0]
    assert "clause_cut" in stored.reasons
    assert "clause_cut" not in stored.observations
    with TestClient(application) as client:
        changed_vote = client.post(
            "/api/v1/feedback",
            json={"review_id": "review-1", "position": 0, "vote": "up"},
        )
    assert changed_vote.json()["feedback_id"] == first.json()["feedback_id"]
    assert changed_vote.json()["scored_steps"] == ["clause_span"]
    assert len(langfuse.scores) == 1
    assert set(langfuse.deleted) == set(first_score_ids)


def test_changed_vote_and_reason_set_delete_scores_no_longer_applicable(api):
    application, store, _service, langfuse = api
    clause = _save_document(store)
    _save_verdict(store, clause)
    with TestClient(application) as client:
        up = client.post(
            "/api/v1/feedback",
            json={"review_id": "review-1", "position": 0, "vote": "up"},
        )
        down = client.post(
            "/api/v1/feedback",
            json={
                "review_id": "review-1",
                "position": 0,
                "vote": "down",
                "reasons": ["verdict_wrong"],
                "corrected_verdict": "valid",
            },
        )
        changed_reason = client.post(
            "/api/v1/feedback",
            json={
                "review_id": "review-1",
                "position": 0,
                "vote": "down",
                "reasons": ["basis_wrong"],
                "corrected_basis": ["bgb-309-7"],
            },
        )

    assert up.json()["scored_steps"] == ["clause_span"]
    assert down.json()["scored_steps"] == ["judge"]
    assert changed_reason.json()["scored_steps"] == ["retrieval"]
    assert len(langfuse.deleted) == 2
    assert {score["observation_id"] for score in langfuse.scores.values()} == {"retrieve-1"}


def test_stale_score_deletion_failure_does_not_reject_feedback(api):
    application, store, _service, langfuse = api
    clause = _save_document(store)
    _save_verdict(store, clause)
    with TestClient(application) as client:
        client.post(
            "/api/v1/feedback",
            json={"review_id": "review-1", "position": 0, "vote": "up"},
        )
        langfuse.delete_error = RuntimeError("Langfuse unavailable")
        changed = client.post(
            "/api/v1/feedback",
            json={
                "review_id": "review-1",
                "position": 0,
                "vote": "down",
                "reasons": ["reasoning_unclear"],
            },
        )

    assert changed.status_code == 200
    assert changed.json()["scored_steps"] == ["explanation"]
    assert store.feedback.for_review("review-1")[0].vote == "down"


def test_upvote_scores_clause_span_and_decision_upserts(api):
    application, store, _service, langfuse = api
    clause = _save_document(store)
    _save_verdict(store, clause)
    with TestClient(application) as client:
        vote = client.post(
            "/api/v1/feedback",
            json={"review_id": "review-1", "position": 0, "vote": "up"},
        )
        first = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "accept", "note": "passt"},
        )
        second = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={
                "action": "override",
                "corrected_verdict": "valid",
                "note": "doch nicht",
            },
        )

    assert vote.json()["scored_steps"] == ["clause_span"]
    assert first.json()["decision"]["action"] == "accept"
    assert second.json()["decision"]["action"] == "override"
    assert store.decisions.for_review("review-1")[0].action == "override"
    assert any(score["observation_id"] == "span-1" for score in langfuse.calls)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (KeyError("missing document"), 404, "not_found"),
        (ValueError("no clauses"), 409, "conflict"),
        (RuntimeError("shutting down"), 503, "service_unavailable"),
    ],
)
def test_review_service_errors_follow_the_protocol(api, error, status, code):
    application, store, service, _langfuse = api
    _save_document(store)
    service.start_error = error
    with TestClient(application) as client:
        response = client.post("/api/v1/reviews", json={"document_id": "doc-1"})
    assert (response.status_code, response.json()["code"]) == (status, code)


def test_idle_sse_disconnect_stops_its_producer_thread():
    store = memory_store()

    def judge(_clause, *, parent=None):  # pragma: no cover - this review is never run
        raise AssertionError("judge should not run")

    judge.agent_version = "agent-1"
    service = build_service(
        store,
        judge,
        build_limiter({}),
        tracer=None,
        recheck_seconds=3600,
    )
    store.reviews.create(
        Review(
            review_id="idle-review",
            document_id="doc-1",
            agent_version="agent-1",
            scope=["liability"],
            status="running",
        )
    )
    events = service.events("idle-review")
    existing_threads = set(threading.enumerate())

    class DisconnectRequest:
        checks = 0

        async def is_disconnected(self) -> bool:
            self.checks += 1
            return self.checks > 1

    async def consume() -> None:
        async for _item in stream_events(DisconnectRequest(), events):
            pass

    asyncio.run(consume())
    new_producers = [
        thread
        for thread in threading.enumerate()
        if thread not in existing_threads and thread.name == "review-sse"
    ]
    assert new_producers == []
    assert service.bus.subscriber_count("idle-review") == 0
    service.shutdown(timeout=0)


def test_real_service_upload_review_feedback_and_cached_rereview():
    store = memory_store()
    langfuse = FakeLangfuse()

    class FakeJudge:
        agent_version = "agent-e2e"

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, clause, *, parent=None) -> ClauseVerdict:
            self.calls += 1
            return ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version=self.agent_version,
                verdict="potentially_void",
                confidence=0.9,
                trace_id="trace-e2e",
                observations=StepObservations(
                    clause_span="span-e2e",
                    judge="judge-e2e",
                    retrieval="retrieval-e2e",
                    explanation="explanation-e2e",
                ),
            )

    judge = FakeJudge()
    limiter = build_limiter({})
    service = build_service(store, judge, limiter, tracer=None)
    application = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=FeedbackService(store, langfuse),
        settings={"ingest": {"max_upload_mb": 20}},
    )

    with TestClient(application) as client:
        upload = client.post("/api/v1/documents", files={"file": ("terms.pdf", _pdf())})
        document_id = upload.json()["document"]["document_id"]
        first = client.post("/api/v1/reviews", json={"document_id": document_id})
        first_id = first.json()["review"]["review_id"]
        first_events = client.get(f"/api/v1/reviews/{first_id}/events")
        findings = client.get(f"/api/v1/reviews/{first_id}/findings")
        feedback = client.post(
            "/api/v1/feedback",
            json={
                "review_id": first_id,
                "position": 0,
                "vote": "down",
                "reasons": ["reasoning_unclear"],
            },
        )
        repeated_upload = client.post(
            "/api/v1/documents", files={"file": ("terms-again.pdf", _pdf())}
        )
        second = client.post("/api/v1/reviews", json={"document_id": document_id})
        second_id = second.json()["review"]["review_id"]
        second_events = client.get(f"/api/v1/reviews/{second_id}/events")
        second_review = client.get(f"/api/v1/reviews/{second_id}")
        repeated_findings = client.get(f"/api/v1/reviews/{second_id}/findings")

    assert upload.status_code == 200
    assert "event: RUN_FINISHED" in first_events.text
    assert findings.json()["findings"][0]["status"] == "done"
    assert feedback.json()["scored_steps"] == ["explanation"]
    assert repeated_upload.json()["reused"] is True
    # The upload page needs this to reopen the stored review instead of starting one.
    assert repeated_upload.json()["latest_review"]["status"] == "finished"
    assert "event: RUN_FINISHED" in second_events.text
    assert second_review.json()["review"]["counts"]["cache_hits"] == 1
    assert repeated_findings.json()["findings"][0]["verdict"]["verdict"] == "potentially_void"
    assert judge.calls == 1


@pytest.mark.parametrize("with_langfuse", [False, True])
def test_build_app_wires_the_real_service_contract(monkeypatch, with_langfuse):
    store = memory_store()
    config = {"review": {"max_concurrency": 2}}
    langfuse = object()
    limiter = object()
    judge = SimpleNamespace(agent_version="agent-wired")
    service = SimpleNamespace(
        limiter=limiter,
        resume_running=lambda: 0,
        shutdown=lambda timeout=None: None,
    )
    calls: dict[str, object] = {}

    monkeypatch.setattr("core.config.load_yaml", lambda _path: config)
    monkeypatch.setattr(
        "core.settings.load_settings",
        lambda: SimpleNamespace(
            langfuse_public_key="pk" if with_langfuse else "",
            langfuse_secret_key="sk" if with_langfuse else "",
        ),
    )
    monkeypatch.setattr("app.store.migrate.apply_pending", lambda **_: None)

    def fake_file_store(root=None):
        calls["files_root"] = root
        return store.files

    monkeypatch.setattr("app.store.files.LocalFileStore", fake_file_store)
    monkeypatch.setattr("app.store.postgres.postgres_store", lambda _files: store)
    monkeypatch.setattr("app.api.main.load_statutes", lambda _path: {})
    checkpointer = SimpleNamespace(saver=object(), close=lambda: None)
    monkeypatch.setattr("app.review.checkpoint.postgres_checkpointer", lambda: checkpointer)

    def fake_langfuse():
        calls["langfuse"] = True
        return langfuse

    def fake_limiter(received_config):
        calls["limiter_config"] = received_config
        return limiter

    def fake_review_settings(received_config):
        calls["settings_config"] = received_config
        return {"max_concurrency": 2, "prices": {"input": 0.0, "output": 0.0}}

    def fake_judge(received_config, **kwargs):
        calls["judge"] = (received_config, kwargs)
        return judge

    def fake_service(*args, **kwargs):
        calls["service"] = (args, kwargs)
        return service

    monkeypatch.setattr("core.langfuse_client.client", fake_langfuse)
    monkeypatch.setattr("app.review.limiter.build_limiter", fake_limiter)
    monkeypatch.setattr("app.review.limiter.review_settings", fake_review_settings)
    monkeypatch.setattr("app.agent.run.make_judge", fake_judge)
    monkeypatch.setattr("app.review.service.build_service", fake_service)
    # Phase 8 generating models: built from the config and the one shared limiter.
    summary_model, chat_model = object(), object()

    def fake_model(model):
        def make(received_config, *, limiter):
            assert received_config is config and limiter is shared
            return model

        return make

    shared = limiter

    monkeypatch.setattr("app.review.summary.make_summary_model", fake_model(summary_model))
    monkeypatch.setattr("app.review.chat.make_chat_model", fake_model(chat_model))

    application = build_app()
    expected_tracer = langfuse if with_langfuse else None
    assert calls.get("langfuse", False) is with_langfuse
    assert calls["limiter_config"] is config
    assert calls["settings_config"] is config
    assert calls["judge"] == (
        config,
        {
            "limiter": limiter,
            "tracer": expected_tracer,
            "caches": store.caches,
            "checkpointer": checkpointer.saver,
        },
    )
    args, kwargs = calls["service"]
    router = kwargs.pop("router")
    assert (args, kwargs) == (
        (store, judge, limiter, expected_tracer),
        {
            "statutes": {},
            "router_model": "jina-embeddings-v3",
            "feedback": application.state.feedback,
            "deliverable": deliverable_settings(config),
            "summary_model": summary_model,
            "chat_model": chat_model,
            "retention_policy": RetentionSettings(),
            "max_concurrency": 2,
            "prices": {"input": 0.0, "output": 0.0},
        },
    )
    # Topics come from the Jina classifier under the configured model, batched.
    assert router.func.__name__ == "classify_topics"
    assert router.keywords == {"model": "jina-embeddings-v3"}
    assert application.state.feedback.langfuse is expected_tracer
    assert calls["files_root"] == ROOT / "stores/files"

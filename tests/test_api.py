"""The HTTP contract is exercised with memory stores and fake external services only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from core.hashing import content_hash
from core.schemas import LawChunk
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.rate_limit import IpRateLimiter
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
from app.review.feedback import FeedbackService
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

    def create_score(self, **kwargs) -> None:
        self.scores[kwargs["score_id"]] = kwargs
        self.calls.append(kwargs)


class FakeReviewService:
    def __init__(self, store) -> None:
        self.store = store
        self.resumed = 0
        self.closed = False
        self.retry_error: Exception | None = None
        self.event_log: list[StoredEvent] = []

    def start(self, document_id, scope) -> Review:
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

    def shutdown(self) -> None:
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
    application = create_app(
        store=store,
        service=service,
        statutes={"bgb-309-7": _statute()},
        feedback=FeedbackService(store, langfuse),
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

    assert first.status_code == 200
    assert first.json()["clauses"]
    assert second.json()["reused"] is True
    assert all(clause["start"] is not None for clause in paste.json()["clauses"])
    assert file_response.content.startswith(b"%PDF-")
    assert file_response.headers["cache-control"].endswith("immutable")
    assert file_response.headers["etag"].startswith('"')


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
        service.retry_error = LookupError("position")
        missing_clause = client.post("/api/v1/reviews/review-1/clauses/99/retry")
        statute = client.get("/api/v1/statutes/bgb-309-7")
        no_statute = client.get("/api/v1/statutes/missing")
        health = client.get("/api/v1/health")
        ready = client.get("/api/v1/ready")

    assert started.status_code == 200
    assert len(findings.json()["findings"]) == 1
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert (conflict.status_code, conflict.json()["code"]) == (409, "not_failed")
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
    assert len(langfuse.scores) == 4


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
    assert first.json()["action"] == "accept"
    assert second.json()["action"] == "override"
    assert store.decisions.for_review("review-1")[0].action == "override"
    assert any(score["observation_id"] == "span-1" for score in langfuse.calls)

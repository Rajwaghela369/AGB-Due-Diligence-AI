"""Decisions are durable state transitions, not merely feedback shortcuts."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

from core.hashing import content_hash
from core.schemas import LawChunk
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain import (
    ClauseSpan,
    ClauseVerdict,
    DecisionIn,
    Document,
    Review,
    Reviewer,
    StepObservations,
)
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.memory import memory_store


class FakeLangfuse:
    def __init__(self) -> None:
        self.scores: dict[str, dict] = {}
        self.api = SimpleNamespace(
            scores=SimpleNamespace(delete=lambda score_id: self.scores.pop(score_id, None))
        )

    def create_score(self, **kwargs) -> None:
        self.scores[kwargs["score_id"]] = kwargs


class Judge:
    agent_version = "agent-decisions"

    def __call__(self, clause, *, parent=None):  # pragma: no cover - seeded tests do not judge
        raise AssertionError("unexpected judge call")


def statute() -> LawChunk:
    return LawChunk(
        chunk_id="bgb-309-7",
        law="BGB",
        paragraph="§ 309",
        item="7",
        title="Haftung",
        citation="§ 309 Nr. 7 BGB",
        topics=["liability"],
        embed_text="Haftung",
        text="Unwirksam ist ein Haftungsausschluss.",
        source_dump="test",
    )


def seeded_service(*, status: str = "done"):
    store = memory_store()
    clause = ClauseSpan(
        position=0,
        text="Die Haftung ist ausgeschlossen.",
        content_hash=content_hash("Die Haftung ist ausgeschlossen."),
    )
    store.documents.save(
        Document(
            document_id="doc-1",
            filename="public.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash=clause.content_hash,
        ),
        [clause],
        {clause.content_hash: clause.text},
    )
    review = store.reviews.create(
        Review(
            review_id="review-1",
            document_id="doc-1",
            agent_version=Judge.agent_version,
            scope=["liability"],
            status="awaiting_decisions" if status == "awaiting_decision" else "finished",
            trace_id="trace-review-1",
        )
    )
    store.reviews.set_clause_status(review.review_id, 0, status)
    store.verdicts.save(
        ClauseVerdict(
            content_hash=clause.content_hash,
            agent_version=review.agent_version,
            verdict="potentially_void",
            legal_basis=["bgb-309-7"],
            confidence=0.9,
            trace_id="trace-review-1",
            observations=StepObservations(
                clause_span="span-1",
                judge="judge-1",
                retrieval="retrieval-1",
            ),
        )
    )
    langfuse = FakeLangfuse()
    feedback = FeedbackService(store, langfuse)
    service = build_service(
        store,
        Judge(),
        build_limiter({}),
        statutes={"bgb-309-7": statute()},
        feedback=feedback,
    )
    return store, service, feedback, langfuse


def test_all_actions_validation_audit_header_and_idle_events() -> None:
    store, service, feedback, langfuse = seeded_service()
    app = create_app(
        store=store,
        service=service,
        statutes={"bgb-309-7": statute()},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}},
    )
    with TestClient(app) as client:
        invalid = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "override"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "invalid_decision"

        accepted = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "accept", "note": "geprüft"},
            headers={"X-Reviewer": quote("Anna Müller")},
        )
        overridden = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "override", "corrected_verdict": "valid"},
        )
        dismissed = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "dismiss"},
        )
        audit = client.get("/api/v1/reviews/review-1/audit?position=0")

    assert [accepted.status_code, overridden.status_code, dismissed.status_code] == [200] * 3
    assert accepted.json()["finding"]["finding_status"] == "accepted"
    assert overridden.json()["finding"]["finding_status"] == "overridden"
    assert dismissed.json()["finding"]["finding_status"] == "dismissed"
    assert len(store.decisions.for_review("review-1")) == 1
    assert len(audit.json()["events"]) == 3
    assert audit.json()["events"][0]["decided_by"] == "Anna Müller"
    assert audit.json()["events"][0]["authenticated"] is False
    assert audit.json()["events"][0]["request_id"]
    assert [event.type for event in store.events.since("review-1")] == [
        "RUN_STARTED",
        "STATE_SNAPSHOT",
        "STATE_DELTA",
        "RUN_FINISHED",
    ] * 3
    assert any(score["name"] == "dismissed" for score in langfuse.scores.values())


def test_not_decidable_is_409() -> None:
    store, service, feedback, _langfuse = seeded_service(status="queued")
    app = create_app(
        store=store,
        service=service,
        statutes={"bgb-309-7": statute()},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}},
    )
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision", json={"action": "accept"}
        )
    assert response.status_code == 409
    assert response.json()["code"] == "not_decidable"


def test_basis_only_override_scores_judge_and_retrieval() -> None:
    store, service, _feedback, langfuse = seeded_service()
    service.statutes["bgb-307"] = object()
    service.decide(
        "review-1",
        0,
        DecisionIn(
            action="override",
            corrected_verdict="potentially_void",
            corrected_basis=["bgb-307"],
        ),
        Reviewer(),
    )
    feedback = store.feedback.for_review("review-1")[0]
    assert feedback.reasons == ["verdict_wrong", "basis_wrong"]
    assert {
        score["observation_id"] for score in langfuse.scores.values() if "observation_id" in score
    } >= {
        "judge-1",
        "retrieval-1",
    }


def test_awaiting_clause_resumes_outside_service_lock_and_finishes_review() -> None:
    _store, service, _feedback, _langfuse = seeded_service(status="awaiting_decision")
    calls = []

    def resume(review, clause, verdict, decision):
        calls.append((review.review_id, clause.position, verdict.verdict))
        assert not service._lock._is_owned()
        return decision.model_copy(update={"reason_de": "Neue Begründung"}), 0.25

    service._resume_decided = resume
    finding = service.decide(
        "review-1",
        0,
        DecisionIn(action="override", corrected_verdict="valid"),
        Reviewer(),
    )
    review = service.get("review-1")
    assert calls == [("review-1", 0, "potentially_void")]
    assert finding.status == "done"
    assert finding.decision.reason_de == "Neue Begründung"
    assert review.status == "finished"
    assert review.cost_usd == 0.25


class RunningJudge:
    agent_version = "agent-running"

    def __init__(self) -> None:
        self.second_started = threading.Event()
        self.release = threading.Event()

    def __call__(self, clause, *, parent=None):
        if clause.position == 1:
            self.second_started.set()
            self.release.wait(2)
        return ClauseVerdict(
            content_hash=clause.content_hash,
            agent_version=self.agent_version,
            verdict="valid",
            confidence=0.9,
            observations=StepObservations(clause_span=f"span-{clause.position}"),
        )


def test_decision_during_run_uses_run_counts_and_bus() -> None:
    store = memory_store()
    clauses = [
        ClauseSpan(position=i, text=f"Klausel {i}", content_hash=content_hash(f"Klausel {i}"))
        for i in range(2)
    ]
    store.documents.save(
        Document(
            document_id="doc-running",
            filename="terms.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash="text",
        ),
        clauses,
        {clause.content_hash: clause.text for clause in clauses},
    )
    judge = RunningJudge()
    feedback = FeedbackService(store, None)
    service = build_service(
        store,
        judge,
        build_limiter({}),
        max_concurrency=1,
        poll_seconds=0.01,
        feedback=feedback,
    )
    review = service.start("doc-running", ["liability"])
    deadline = time.monotonic() + 2
    while store.reviews.clause_statuses(review.review_id).get(0, (None,))[0] != "done":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert judge.second_started.wait(2)
    before = list(store.events.since(review.review_id))[-1].seq
    finding = service.decide(review.review_id, 0, DecisionIn(action="accept"), Reviewer())
    emitted = list(store.events.since(review.review_id, before))
    assert finding.decision is not None
    assert [event.type for event in emitted] == ["STATE_DELTA", "CUSTOM"]
    assert store.reviews.get(review.review_id).counts.decided == 1
    judge.release.set()
    assert service.wait(review.review_id, 2)

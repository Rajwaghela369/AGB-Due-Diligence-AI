"""The Phase 7 Wave 0 contracts behave as documented, before Wave 1 builds on them."""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.domain import (
    IDLE_REVIEW_STATUSES,
    ClauseSpan,
    ClauseVerdict,
    Decision,
    DecisionIn,
    Finding,
    Interrupt,
    Reviewer,
    decided_verdict,
    kept_model_verdict,
)
from app.review.runner import count
from app.review.service import build_service
from app.store.memory import memory_store
from tests.test_api import _save_document, _save_verdict
from tests.test_api import api as api  # noqa: F401 - the fixture
from tests.test_review_runner import FakeJudge, StubLimiter


def _decision(action, corrected=None, basis=(), model="potentially_void") -> Decision:
    return Decision(
        review_id="r",
        position=0,
        action=action,
        corrected_verdict=corrected,
        corrected_basis=list(basis),
        model_verdict=model,
        model_basis=["bgb-309-7"],
    )


@pytest.mark.parametrize(
    ("decision", "in_force", "kept", "finding_status"),
    [
        (_decision("accept"), ("potentially_void", ["bgb-309-7"]), True, "accepted"),
        (_decision("override", "valid"), ("valid", ["bgb-309-7"]), False, "overridden"),
        (
            _decision("override", "potentially_void", ["bgb-307"]),
            ("potentially_void", ["bgb-307"]),
            True,  # only the § changed: still agreement
            "overridden",
        ),
        (_decision("dismiss"), (None, []), None, "dismissed"),
    ],
)
def test_decision_helpers_and_finding_status(decision, in_force, kept, finding_status):
    assert decided_verdict(decision) == in_force
    assert kept_model_verdict(decision) is kept
    clause = ClauseSpan(position=0, text="t", content_hash="h")
    finding = Finding(position=0, clause=clause, status="done", decision=decision)
    assert finding.finding_status == finding_status
    assert Finding(position=0, clause=clause, status="done").finding_status == "open"


def _verdict(kind: str) -> ClauseVerdict:
    return ClauseVerdict(content_hash=kind, agent_version="v", verdict=kind, confidence=0.5)


def test_counts_tally_awaiting_and_decisions():
    entries = {
        0: ("done", None, _verdict("potentially_void")),
        1: ("done", None, _verdict("potentially_void")),
        2: ("awaiting_decision", None, _verdict("uncertain")),
        3: ("done", None, _verdict("uncertain")),
        4: ("done", None, _verdict("valid")),
        5: ("failed", "x", None),
        6: ("checking", None, _verdict("valid")),  # a verdict mid-resume is not counted
        7: ("queued", None, None),
        8: ("out_of_scope", None, None),
    }
    decisions = {1: _decision("accept"), 3: _decision("dismiss"), 4: _decision("accept")}

    counts = count(entries, cache_hits=2, decisions=decisions)

    assert counts.model_dump() == {
        "total": 8,
        "done": 4,
        "failed": 1,
        "awaiting_decision": 1,
        "potentially_void": 2,
        "uncertain": 2,
        "valid": 1,
        "out_of_scope": 1,
        "cache_hits": 2,
        "decided": 3,
        "open_void": 1,
        "open_review": 1,
    }
    assert count(entries, 0).decided == 0


def test_interrupt_value_and_idle_statuses():
    value = Interrupt(position=3, verdict="uncertain", confidence=0.41).model_dump(mode="json")
    assert set(value) == {"position", "verdict", "confidence", "question"}
    assert value["question"]
    assert "running" not in IDLE_REVIEW_STATUSES and "awaiting_decisions" in IDLE_REVIEW_STATUSES


def test_wave_1_service_methods_use_the_store_contracts():
    service = build_service(memory_store(), FakeJudge(), StubLimiter())
    with pytest.raises(KeyError, match="unknown review"):
        service.decide("r", 0, DecisionIn(action="accept"), Reviewer(name="Anna"))
    with pytest.raises(KeyError, match="unknown review"):
        service.audit("r")
    assert service.agreement().model_dump(exclude={"by_verdict"}) == {
        "agent_version": "agent-v1",
        "decided": 0,
        "kept": 0,
        "overridden": 0,
        "dismissed": 0,
        "rate": None,
        "last_decided_at": None,
    }


def test_decision_records_the_reviewer_header_and_returns_progress(api):  # noqa: F811
    application, store, _service, _langfuse = api
    clause = _save_document(store)
    _save_verdict(store, clause)
    with TestClient(application) as client:
        response = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "accept"},
            headers={"X-Reviewer": quote("Anna Müller")},
        )
        anonymous = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "dismiss"},
            headers={"X-Reviewer": "   "},
        )
        too_long = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "accept"},
            headers={"X-Reviewer": "x" * 101},
        )
        bad_encoding = client.put(
            "/api/v1/reviews/review-1/clauses/0/decision",
            json={"action": "accept"},
            headers={"X-Reviewer": "%FF"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision"]["decided_by"] == "Anna Müller"
    assert body["finding"]["position"] == 0
    assert set(body["review"]) >= {"review", "decidable", "progress", "can_complete"}
    assert anonymous.json()["decision"]["decided_by"] is None
    assert (too_long.status_code, too_long.json()["code"]) == (422, "invalid_reviewer")
    assert (bad_encoding.status_code, bad_encoding.json()["code"]) == (422, "invalid_reviewer")
    history = store.decisions.history("review-1")
    assert [(e.action, e.decided_by, e.authenticated) for e in history] == [
        ("accept", "Anna Müller", False),
        ("dismiss", None, False),
    ]
    assert history[0].request_id  # the request log middleware's id


def test_review_out_progress_fields(api):  # noqa: F811
    application, store, service, _langfuse = api
    clause = _save_document(store)
    review = _save_verdict(store, clause)
    counts = review.counts.model_copy(
        update={
            "total": 4,
            "done": 4,
            "potentially_void": 1,
            "uncertain": 1,
            "valid": 2,
            "decided": 3,
            "open_void": 0,
            "open_review": 1,
        }
    )
    store.reviews.update_counts(review.review_id, counts, 0.0)
    with TestClient(application) as client:
        body = client.get(f"/api/v1/reviews/{review.review_id}").json()
        store.reviews.update_counts(
            review.review_id, counts.model_copy(update={"decided": 4, "open_review": 0}), 0.0
        )
        done = client.get(f"/api/v1/reviews/{review.review_id}").json()

    assert (body["decidable"], body["progress"], body["can_complete"]) == (4, 0.75, False)
    assert (done["progress"], done["can_complete"]) == (1.0, True)


def test_cors_lets_the_ui_send_the_reviewer_header():
    from app.api.main import create_app
    from app.review.feedback import FeedbackService

    store = memory_store()
    application = create_app(
        store=store,
        service=build_service(store, FakeJudge(), StubLimiter()),
        statutes={},
        feedback=FeedbackService(store, None),
        settings={"api": {"cors_origins": ["http://localhost:5173"]}},
    )
    with TestClient(application) as client:
        preflight = client.options(
            "/api/v1/reviews/r/clauses/0/decision",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "x-reviewer",
            },
        )
    assert preflight.status_code == 200
    assert "x-reviewer" in preflight.headers["access-control-allow-headers"].lower()

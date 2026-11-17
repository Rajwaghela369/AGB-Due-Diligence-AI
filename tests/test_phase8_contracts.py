"""The Phase 8 Wave 0 contracts behave as documented, before Wave 1 builds on them.

The summary, export and chat bodies are stubs: the routes exist and answer 501, the
models serialise, the error mappings and the SSE framing work with a stand-in service,
and the cost and time figures (8.D) are derived from stored rows and config.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from core.paths import CONFIGS
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.costs import pricing, wall_time_ms
from app.api.main import create_app
from app.api.schemas import ExportBlockedBody, Pricing
from app.domain import (
    CHAT_DISCLAIMER,
    DRAFT_WATERMARK,
    EXPORT_MEDIA_TYPES,
    ChatEvent,
    ChatIn,
    ChatResult,
    DiffSegment,
    ExportBlocker,
    ExportFile,
    Review,
    Reviewer,
    ReviewSummary,
    SummaryCounts,
    SummaryFinding,
    SummaryGroup,
)
from app.review.deliverable import DeliverableSettings, deliverable_settings
from app.review.feedback import FeedbackService
from app.review.interfaces import ExportBlocked, NotChattable
from app.review.service import build_service
from app.store.memory import memory_store
from tests.test_api import _save_document, _save_verdict
from tests.test_review_runner import FakeJudge, StubLimiter

PRICED = {
    "review": {"price_usd_per_million_tokens": {"input": 2.0, "output": 10.0}, "eur_per_usd": 0.5}
}


def _app(settings=None):
    store = memory_store()
    service = build_service(store, FakeJudge(), StubLimiter())
    application = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=FeedbackService(store, None),
        settings=settings or {},
    )
    return application, store, service


def _finding(position=2, verdict="potentially_void", status="accepted") -> SummaryFinding:
    return SummaryFinding(
        position=position,
        number="6.1",
        heading="§ 6 Haftung",
        excerpt="Die Haftung ist ausgeschlossen …",
        verdict=verdict,
        basis=["bgb-309-7-a"],
        citations=["§ 309 Nr. 7 lit. a BGB"],
        model_verdict="potentially_void",
        confidence=0.91,
        finding_status=status,
        decided_by="Anna Müller",
    )


# --- models ----------------------------------------------------------------------


def test_summary_round_trips_through_json():
    summary = ReviewSummary(
        review_id="r",
        document_id="d",
        filename="agb.pdf",
        agent_version="v",
        review_status="finished",
        draft=False,
        counts=SummaryCounts(findings=3, potentially_void=1, valid=2, accepted=3),
        void_groups=[SummaryGroup(key="bgb-309", label="§ 309 BGB", findings=[_finding()])],
        top_risks=[_finding()],
        recommendation="Klausel 6.1 streichen.",
        decision_set_hash="0" * 64,
        reviewers=["Anna Müller"],
        generated_at=datetime(2026, 11, 17, tzinfo=UTC),
    )
    dumped = summary.model_dump(mode="json")
    assert dumped["recommendation_from_llm"] is False
    assert dumped["void_groups"][0]["findings"][0]["overridden_to_void"] is False
    assert ReviewSummary.model_validate_json(summary.model_dump_json()) == summary


def test_export_contract_shapes():
    blocked = ExportBlocked(
        "undecided", [ExportBlocker(position=2, number="6.1", verdict="uncertain")]
    )
    body = ExportBlockedBody(
        code="export_blocked", message="m", reason=blocked.reason, blocking=blocked.blocking
    )
    assert body.model_dump(mode="json")["blocking"] == [
        {"position": 2, "number": "6.1", "heading": None, "verdict": "uncertain"}
    ]
    assert isinstance(blocked, ValueError) and ExportBlocked("not_finished", []).blocking == []
    assert set(EXPORT_MEDIA_TYPES) == {"docx", "md", "csv"} and DRAFT_WATERMARK == "Entwurf"


def test_chat_contract_shapes():
    assert ChatIn(message="Formuliere 6.1 um").history == []
    with pytest.raises(ValidationError):
        ChatIn(message="")
    with pytest.raises(ValidationError):
        ChatIn(message="x" * 2001)
    with pytest.raises(ValidationError):
        ChatIn(message="x", history=[{"role": "system", "content": "y"}])

    result = ChatResult(
        message_id="m1",
        text="Vorschlag: …",
        suggestion="Die Haftung für Vorsatz bleibt unberührt.",
        diff=[DiffSegment(op="delete", text="ist "), DiffSegment(op="insert", text="bleibt ")],
        model="gemini-3.8-flash",
    )
    assert result.disclaimer == CHAT_DISCLAIMER
    assert ChatResult.model_validate(result.model_dump(mode="json")) == result
    assert ChatEvent(type="TEXT_MESSAGE_CONTENT", payload={"delta": "x"}).type


def test_settings_default_to_no_llm_summary_and_read_the_shipped_config():
    from core.config import load_yaml

    assert DeliverableSettings().summary.llm_enabled is False
    shipped = deliverable_settings(load_yaml(CONFIGS / "app.yaml"))
    assert shipped.summary.llm_enabled is False  # Decisions taken #4
    assert shipped.chat.max_answer_chars > 0 and shipped.chat.max_output_tokens > 0
    assert shipped.export.template_dir == CONFIGS / "export"
    assert (shipped.export.template_dir / "README.md").exists()
    # models.summary / models.chat fall back to models.explain
    fallback = deliverable_settings({"models": {"explain": {"name": "e"}}})
    assert fallback.summary.model == {"name": "e"} and fallback.chat.model == {"name": "e"}


# --- Wave 1 stubs -------------------------------------------------------------------


def test_service_stubs_raise_not_implemented():
    _application, _store, service = _app()
    with pytest.raises(NotImplementedError):
        service.summary("r")
    with pytest.raises(NotImplementedError):
        service.export("r", "md", reviewer=Reviewer())
    with pytest.raises(NotImplementedError):
        service.chat("r", 0, ChatIn(message="x"), Reviewer())
    assert service.summary_model is None and service.chat_model is None
    assert service.deliverable.summary.llm_enabled is False


def test_stub_routes_answer_501():
    application, _store, _service = _app()
    with TestClient(application) as client:
        summary = client.get("/api/v1/reviews/r/summary")
        export = client.get("/api/v1/reviews/r/export?format=docx&draft=true")
        chat = client.post("/api/v1/reviews/r/clauses/0/chat", json={"message": "x"})
        bad_format = client.get("/api/v1/reviews/r/export?format=pdf")
        bad_chat = client.post("/api/v1/reviews/r/clauses/0/chat", json={"message": ""})

    for response in (summary, export, chat):
        assert response.status_code == 501
        assert response.json()["code"] == "not_implemented"
    assert bad_format.status_code == 422 and bad_format.json()["code"] == "invalid_request"
    assert bad_chat.status_code == 422


# --- route mappings with a stand-in service ------------------------------------------


def test_export_route_maps_blocked_and_sends_the_file():
    application, _store, service = _app()
    seen = {}

    def export(review_id, fmt, *, draft=False, reviewer):
        seen.update(fmt=fmt, draft=draft, reviewer=reviewer)
        if review_id == "missing":
            raise KeyError(review_id)
        if not draft:
            raise ExportBlocked("undecided", [ExportBlocker(position=1, verdict="uncertain")])
        return ExportFile(
            filename="AGB-Check_Müller_Entwurf.csv",
            media_type=EXPORT_MEDIA_TYPES[fmt],
            content=b"\xef\xbb\xbfposition;entwurf\r\n",
            draft=True,
        )

    service.export = export
    with TestClient(application) as client:
        blocked = client.get("/api/v1/reviews/r/export?format=csv")
        missing = client.get("/api/v1/reviews/missing/export?format=csv&draft=true")
        draft = client.get(
            "/api/v1/reviews/r/export?format=csv&draft=true",
            headers={"X-Reviewer": "Anna%20M%C3%BCller"},
        )

    assert blocked.status_code == 409
    assert blocked.json() == {
        "code": "export_blocked",
        "message": blocked.json()["message"],
        "reason": "undecided",
        "blocking": [{"position": 1, "number": None, "heading": None, "verdict": "uncertain"}],
    }
    assert missing.status_code == 404
    assert draft.status_code == 200
    assert draft.headers["content-type"] == "text/csv; charset=utf-8"
    assert draft.headers["x-export-draft"] == "true"
    assert draft.headers["content-disposition"] == (
        "attachment; filename*=UTF-8''AGB-Check_M%C3%BCller_Entwurf.csv"
    )
    assert draft.content.startswith(b"\xef\xbb\xbf")
    assert seen == {"fmt": "csv", "draft": True, "reviewer": Reviewer(name="Anna Müller")}


def test_chat_route_streams_events_without_ids_and_maps_errors():
    application, _store, service = _app()

    def chat(review_id, position, message, reviewer, *, request_id=None):
        if position == 9:
            raise KeyError(position)
        if position == 8:
            raise NotChattable("Klausel ohne Ergebnis")
        return iter(
            [
                ChatEvent(type="RUN_STARTED", payload={"type": "RUN_STARTED"}),
                ChatEvent(type="TEXT_MESSAGE_CONTENT", payload={"delta": "Vorschlag: „neu“"}),
                ChatEvent(type="RUN_FINISHED", payload={"type": "RUN_FINISHED"}),
            ]
        )

    service.chat = chat
    with TestClient(application) as client:
        streamed = client.post("/api/v1/reviews/r/clauses/0/chat", json={"message": "x"})
        missing = client.post("/api/v1/reviews/r/clauses/9/chat", json={"message": "x"})
        no_verdict = client.post("/api/v1/reviews/r/clauses/8/chat", json={"message": "x"})

    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    messages = [m for m in streamed.text.split("\n\n") if m]
    assert [m.splitlines()[0] for m in messages] == [
        "event: RUN_STARTED",
        "event: TEXT_MESSAGE_CONTENT",
        "event: RUN_FINISHED",
    ]
    assert "id:" not in streamed.text
    assert json.loads(messages[1].splitlines()[1][len("data: ") :]) == {"delta": "Vorschlag: „neu“"}
    assert missing.status_code == 404
    assert no_verdict.status_code == 409 and no_verdict.json()["code"] == "not_chattable"


def test_cors_exposes_the_export_headers():
    application, _store, _service = _app({"api": {"cors_origins": ["http://localhost:5173"]}})
    with TestClient(application) as client:
        response = client.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "content-disposition" in exposed and "x-export-draft" in exposed


# --- cost and time (8.D) -------------------------------------------------------------


def test_pricing_is_unconfirmed_while_the_prices_are_placeholders():
    assert pricing({}) == Pricing()
    assert pricing({"review": {"price_usd_per_million_tokens": {"input": 0, "output": 0}}}) == (
        Pricing(confirmed=False)
    )
    assert pricing(PRICED) == Pricing(
        input_usd_per_million=2.0, output_usd_per_million=10.0, eur_per_usd=0.5, confirmed=True
    )


def test_wall_time_runs_from_first_start_to_the_end_of_the_last_run():
    start = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
    review = Review(
        review_id="r", document_id="d", agent_version="v", scope=["liability"], status="queued"
    )
    assert wall_time_ms(review) is None
    running = review.model_copy(update={"status": "running", "started_at": start})
    assert wall_time_ms(running, now=start + timedelta(seconds=3)) == 3000
    finished = running.model_copy(
        update={"status": "awaiting_decisions", "finished_at": start + timedelta(seconds=42)}
    )
    assert wall_time_ms(finished, now=start + timedelta(days=1)) == 42000


def test_review_and_home_list_carry_cost_and_time():
    application, store, _service = _app(PRICED)
    clause = _save_document(store)
    review = _save_verdict(store, clause)
    store.reviews.update_counts(
        review.review_id, review.counts.model_copy(update={"total": 4, "cache_hits": 1}), 0.25
    )
    with TestClient(application) as client:
        body = client.get(f"/api/v1/reviews/{review.review_id}").json()
        home = client.get("/api/v1/documents").json()

    assert body["pricing"] == {
        "input_usd_per_million": 2.0,
        "output_usd_per_million": 10.0,
        "eur_per_usd": 0.5,
        "confirmed": True,
    }
    assert (body["cost_eur"], body["cache_hit_rate"]) == (0.125, 0.25)
    item = home["items"][0]
    assert (item["cost_eur"], item["cache_hit_rate"]) == (0.125, 0.25)
    assert item["wall_time_ms"] == body["wall_time_ms"]
    assert home["pricing"] == body["pricing"]

"""Freeze the additive AVV contracts before the knowledge-base, agent and UI waves split.

The tests intentionally exercise only local models, stores and fakes. They prove the two
profiles cannot collide in the document cache, all eight legal obligations are configured,
and Wave 1 reaches explicit stubs rather than accidentally using the AGB router.
"""

from pathlib import Path

import pytest
from core.config import load_yaml
from core.hashing import content_hash
from fastapi.testclient import TestClient

from app.agent.run import Judge
from app.api.main import create_app
from app.domain import (
    EVENT_SCHEMA_VERSION,
    ClauseSpan,
    Document,
    Finding,
    MissingObligation,
    Obligation,
    ObligationFinding,
    Review,
    blob_key,
    document_id_for,
)
from app.review.feedback import FeedbackService
from app.review.interfaces import ReviewService
from app.review.service import build_service
from app.store.memory import memory_store
from tests.test_review_runner import FakeJudge, StubLimiter

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_yaml(ROOT / "configs" / "profiles" / "avv.yaml")


def _document(document_id: str = "d" * 64, **values) -> Document:
    return Document(
        document_id=document_id,
        filename="vertrag.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash="text",
        **values,
    )


def test_document_type_defaults_to_agb_and_is_copied_to_reviews():
    document = _document()
    review = Review(
        review_id="r",
        document_id=document.document_id,
        agent_version="v1",
        scope=[],
        status="queued",
    )

    assert document.document_type == "agb"
    assert review.document_type == "agb"
    assert review.obligations == []


def test_avv_identity_is_distinct_but_the_source_blob_is_shared():
    source_hash = "a" * 64
    agb_id = document_id_for(source_hash)
    avv_id = document_id_for(source_hash, document_type="avv")
    agb = _document(agb_id, source_hash=source_hash)
    avv = _document(avv_id, source_hash=source_hash, document_type="avv")

    assert agb_id == source_hash
    assert avv_id != agb_id
    assert document_id_for(source_hash, document_type="avv") == avv_id
    assert blob_key(agb) == blob_key(avv) == f"{source_hash}.pdf"


def test_avv_profile_has_the_eight_exact_questions_and_collection_alias():
    obligations = [Obligation(**value) for value in PROFILE["obligations"]]

    assert PROFILE["profile"] == PROFILE["document_type"] == "avv"
    assert PROFILE["collection_alias"] == "dsgvo_avv"
    assert [item.code for item in obligations] == list("abcdefgh")
    assert [item.label_de for item in obligations] == [
        "Weisungsbindung",
        "Vertraulichkeit",
        "Sicherheit der Verarbeitung",
        "Unterauftragsverarbeiter",
        "Betroffenenrechte",
        "Unterstützungspflichten",
        "Löschung und Rückgabe",
        "Nachweise und Kontrollen",
    ]
    assert [item.question_de for item in obligations] == [
        f"Erfüllt die Klausel Art. 28 Abs. 3 lit. {code}?" for code in "abcdefgh"
    ]


def test_clause_and_document_level_obligation_findings_are_separate():
    clause = ClauseSpan(position=0, text="TOM nach Art. 32", content_hash="h")
    answer = ObligationFinding(
        obligation="c",
        fulfilled=True,
        confidence=0.9,
        legal_basis=["dsgvo-28-3-c"],
    )
    finding = Finding(
        position=0,
        clause=clause,
        status="done",
        obligation_findings=[answer],
    )
    missing = MissingObligation(
        review_id="r",
        obligation=Obligation(**PROFILE["obligations"][3]),
        reason_de="Art. 28 Abs. 3 lit. d wird im Dokument nicht erwähnt.",
    )

    assert finding.obligation_findings == [answer]
    assert missing.obligation.code == "d"
    assert missing.finding_status == "open"
    with pytest.raises(ValueError):
        ObligationFinding.model_validate({**answer.model_dump(), "confidence": 1.1})


def test_wave_1_agent_and_service_boundaries_are_explicit_stubs():
    clause = ClauseSpan(position=0, text="Vertraulichkeit", content_hash="h")

    with pytest.raises(NotImplementedError):
        Judge.route_obligations(None, clause)  # type: ignore[arg-type]
    with pytest.raises(NotImplementedError):
        ReviewService.missing_obligations(None, "r")  # type: ignore[arg-type]


def test_memory_store_round_trips_multi_label_routes():
    store = memory_store()
    document = _document(document_type="avv")
    clause = ClauseSpan(
        position=0,
        text="Weisungen und Vertraulichkeit",
        content_hash=content_hash("Weisungen und Vertraulichkeit"),
    )
    store.documents.save(document, [clause], {clause.content_hash: clause.text})
    review = store.reviews.create(
        Review(
            review_id="r",
            document_id=document.document_id,
            agent_version="avv-v1",
            document_type="avv",
            scope=[],
            obligations=list("abcdefgh"),
            status="queued",
        )
    )
    routed = {clause.content_hash: ["a", "b"]}

    store.caches.put_obligations("router", "avv", routed)
    store.reviews.set_clause_obligations(review.review_id, {0: ["a", "b"]})

    assert store.caches.get_obligations([clause.content_hash], "router", "avv") == routed
    assert store.reviews.clause_obligations(review.review_id) == {0: ["a", "b"]}


def test_json_upload_accepts_avv_and_keeps_agb_as_the_default():
    store = memory_store()
    service = build_service(store, FakeJudge(), StubLimiter())
    application = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=FeedbackService(store, None),
        settings={},
    )

    with TestClient(application) as client:
        avv = client.post(
            "/api/v1/documents",
            json={
                "text": "§ 1 Weisungen gelten.\n§ 2 Vertraulichkeit gilt.",
                "document_type": "avv",
            },
        )
        agb = client.post(
            "/api/v1/documents",
            json={"text": "§ 1 Haftung gilt.\n§ 2 Gewährleistung gilt."},
        )

    assert avv.status_code == 200 and avv.json()["document"]["document_type"] == "avv"
    assert agb.status_code == 200 and agb.json()["document"]["document_type"] == "agb"
    assert store.documents.uploads[0]["document_type"] == "avv"


def test_migration_and_event_contract_cover_avv_storage():
    sql = (ROOT / "app" / "store" / "migrations" / "009_phase10_avv.sql").read_text()
    events = (ROOT / "docs" / "Events.md").read_text()

    assert EVENT_SCHEMA_VERSION == 2
    assert "create table obligation_routes" in sql
    assert sql.count("document_type") >= 3
    assert "obligation_findings jsonb" in sql
    assert "replace /missing_obligations" in events

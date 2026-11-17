"""The Phase 8 summary is a stable projection of findings, not a second review."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from core.budget import BudgetExceeded
from core.hashing import content_hash
from core.llm import Completion
from core.schemas import LawChunk

from app.domain import ClauseSpan, ClauseVerdict, Decision, Document, Review
from app.review import summary as summary_module
from app.review.deliverable import DeliverableSettings, SummarySettings
from app.review.service import build_service
from app.store.memory import memory_store

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


class Judge:
    agent_version = "agent-summary-v1"

    def __call__(self, clause, *, parent=None):  # pragma: no cover - all results are seeded
        raise AssertionError("summary must not judge clauses")


def _statute(chunk_id: str, paragraph: str, citation: str, text: str) -> LawChunk:
    return LawChunk(
        chunk_id=chunk_id,
        law="BGB",
        paragraph=paragraph,
        title="Testnorm",
        citation=citation,
        topics=["liability"],
        embed_text=citation,
        text=text,
        source_dump="test",
    )


def seeded_service(*, decide_uncertain: bool = False, summary_model=None):
    store = memory_store()
    texts = [
        "Die Haftung für Körper- und Gesundheitsschäden ist vollständig ausgeschlossen.",
        "Eine Rückzahlung ist auch bei einer wesentlichen Pflichtverletzung ausgeschlossen.",
        "Für jede Reklamation gilt eine sehr kurze Ausschlussfrist.",
        "Die gesetzliche Gewährleistung bleibt unberührt.",
        "Diese Klausel konnte technisch nicht geprüft werden.",
        "Allgemeine Angaben zum Vertragspartner.",
    ]
    clauses = [
        ClauseSpan(
            position=position,
            number=f"{position + 1}.1",
            heading=f"§ {position + 1} Regelung",
            text=text,
            content_hash=content_hash(text),
            in_scope=position != 5,
        )
        for position, text in enumerate(texts)
    ]
    store.documents.save(
        Document(
            document_id="doc-summary",
            filename="Muster-AGB.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash=content_hash("".join(texts)),
            created_at=NOW,
        ),
        clauses,
        {clause.content_hash: clause.text for clause in clauses},
    )
    store.reviews.create(
        Review(
            review_id="review-summary",
            document_id="doc-summary",
            agent_version=Judge.agent_version,
            scope=["liability"],
            status="finished" if decide_uncertain else "awaiting_decisions",
            created_at=NOW,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    statuses = ["done", "done", "done", "done", "failed", "out_of_scope"]
    for clause, status in zip(clauses, statuses, strict=True):
        store.reviews.set_clause_status("review-summary", clause.position, status)
    verdicts = [
        (0, "potentially_void", ["bgb-309-7"], 0.8, "Haftungsausschluss unwirksam."),
        (1, "valid", [], 0.6, "Modell hielt die Klausel für wirksam."),
        (2, "uncertain", ["bgb-307"], 0.9, "Die Frist muss geprüft werden."),
        (3, "valid", [], 0.99, "Die Klausel bestätigt das Gesetz."),
    ]
    for position, verdict, basis, confidence, reason in verdicts:
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clauses[position].content_hash,
                agent_version=Judge.agent_version,
                verdict=verdict,
                legal_basis=basis,
                reason_de=reason,
                confidence=confidence,
            )
        )
    decisions = [
        Decision(
            review_id="review-summary",
            position=0,
            action="accept",
            note="bestätigt",
            decided_by="Anna Müller",
            decided_at=NOW,
            model_verdict="potentially_void",
            model_basis=["bgb-309-7"],
        ),
        Decision(
            review_id="review-summary",
            position=1,
            action="override",
            corrected_verdict="potentially_void",
            corrected_basis=["bgb-307"],
            note="Risiko ergänzt",
            decided_by="Bernd König",
            decided_at=NOW,
            model_verdict="valid",
        ),
        Decision(
            review_id="review-summary",
            position=3,
            action="dismiss",
            note="nur deklaratorisch",
            decided_by="Anna Müller",
            decided_at=NOW,
            model_verdict="valid",
        ),
    ]
    if decide_uncertain:
        decisions.append(
            Decision(
                review_id="review-summary",
                position=2,
                action="accept",
                note="zur weiteren Prüfung übernommen",
                decided_by="Anna Müller",
                decided_at=NOW,
                model_verdict="uncertain",
                model_basis=["bgb-307"],
            )
        )
    for decision in decisions:
        store.decisions.put(decision, agent_version=Judge.agent_version)
    statutes = {
        "bgb-307": _statute(
            "bgb-307", "§ 307", "§ 307 BGB", "Unangemessene Benachteiligungen sind unwirksam."
        ),
        "bgb-309-7": _statute(
            "bgb-309-7",
            "§ 309",
            "§ 309 Nr. 7 BGB",
            "Ein Ausschluss für Körper- und Gesundheitsschäden ist unwirksam.",
        ),
    }
    settings = DeliverableSettings(
        summary=SummarySettings(
            llm_enabled=summary_model is not None,
            top_risks=5,
            excerpt_chars=60,
            model={"provider": "fake", "name": "summary-fake"},
        )
    )
    service = build_service(
        store,
        Judge(),
        object(),
        statutes=statutes,
        deliverable=settings,
        summary_model=summary_model,
    )
    return store, service


def test_summary_snapshot_and_decision_change(monkeypatch) -> None:
    monkeypatch.setattr("app.review.summary._now", lambda: NOW)
    store, service = seeded_service()
    summary = service.summary("review-summary")

    snapshot = summary.model_dump(mode="json")
    snapshot["decision_set_hash"] = "<sha256>"
    assert snapshot == {
        "review_id": "review-summary",
        "document_id": "doc-summary",
        "filename": "Muster-AGB.pdf",
        "agent_version": "agent-summary-v1",
        "review_status": "awaiting_decisions",
        "draft": True,
        "counts": {
            "findings": 4,
            "potentially_void": 2,
            "uncertain": 1,
            "valid": 0,
            "accepted": 1,
            "overridden": 1,
            "dismissed": 1,
            "open": 1,
            "open_void": 0,
            "open_review": 1,
            "failed": 1,
            "out_of_scope": 1,
        },
        "void_groups": [
            {
                "key": "bgb-307",
                "label": "§ 307 BGB",
                "findings": [
                    {
                        "position": 1,
                        "number": "2.1",
                        "heading": "§ 2 Regelung",
                        "excerpt": "Eine Rückzahlung ist auch bei einer wesentlichen…",
                        "verdict": "potentially_void",
                        "basis": ["bgb-307"],
                        "citations": ["§ 307 BGB"],
                        "model_verdict": "valid",
                        "confidence": 0.6,
                        "finding_status": "overridden",
                        "overridden_to_void": True,
                        "decided_by": "Bernd König",
                        "note": "Risiko ergänzt",
                    }
                ],
            },
            {
                "key": "bgb-309",
                "label": "§ 309 BGB",
                "findings": [
                    {
                        "position": 0,
                        "number": "1.1",
                        "heading": "§ 1 Regelung",
                        "excerpt": "Die Haftung für Körper- und Gesundheitsschäden ist…",
                        "verdict": "potentially_void",
                        "basis": ["bgb-309-7"],
                        "citations": ["§ 309 Nr. 7 BGB"],
                        "model_verdict": "potentially_void",
                        "confidence": 0.8,
                        "finding_status": "accepted",
                        "overridden_to_void": False,
                        "decided_by": "Anna Müller",
                        "note": "bestätigt",
                    }
                ],
            },
        ],
        "top_risks": [
            snapshot["void_groups"][1]["findings"][0],
            snapshot["void_groups"][0]["findings"][0],
            {
                "position": 2,
                "number": "3.1",
                "heading": "§ 3 Regelung",
                "excerpt": "Für jede Reklamation gilt eine sehr kurze Ausschlussfrist.",
                "verdict": "uncertain",
                "basis": ["bgb-307"],
                "citations": ["§ 307 BGB"],
                "model_verdict": "uncertain",
                "confidence": 0.9,
                "finding_status": "open",
                "overridden_to_void": False,
                "decided_by": None,
                "note": "",
            },
        ],
        "recommendation": "2 Klauseln sollten überarbeitet oder entfernt werden; 1 Klausel "
        "benötigt eine weitere rechtliche Prüfung. Schwerpunkte sind § 307 BGB, § 309 BGB. "
        "Vor dem finalen Export sind die offenen Feststellungen zu entscheiden.",
        "recommendation_from_llm": False,
        "decision_set_hash": "<sha256>",
        "reviewers": ["Anna Müller", "Bernd König"],
        "last_decided_at": "2026-11-17T12:00:00Z",
        "generated_at": "2026-11-17T12:00:00Z",
    }
    first_hash = summary.decision_set_hash
    open_finding = next(
        finding for finding in service.findings("review-summary") if finding.position == 2
    )
    store.decisions.put(
        Decision(
            review_id="review-summary",
            position=2,
            action="accept",
            note="entschieden",
            decided_by="Anna Müller",
            decided_at=NOW,
            model_verdict=open_finding.verdict.verdict,
            model_basis=open_finding.verdict.legal_basis,
        ),
        agent_version=Judge.agent_version,
    )
    assert service.summary("review-summary").decision_set_hash != first_hash


def test_llm_recommendation_is_called_once_and_cached(monkeypatch) -> None:
    monkeypatch.setattr("app.review.summary._now", lambda: NOW)
    calls = []

    def model(system: str, user: str) -> Completion:
        calls.append((system, user))
        return Completion(
            text='{"recommendation":"Gecachte Empfehlung."}',
            input_tokens=10,
            output_tokens=3,
            model="summary-fake",
        )

    _store, service = seeded_service(summary_model=model)
    first = service.summary("review-summary")
    second = service.summary("review-summary")
    assert first.recommendation == second.recommendation == "Gecachte Empfehlung."
    assert first.recommendation_from_llm is second.recommendation_from_llm is True
    assert len(calls) == 1


def test_model_failure_keeps_rule_based_recommendation() -> None:
    summary_module._failure_until.clear()

    def model(system: str, user: str) -> Completion:
        return Completion("", 0, 0, "summary-fake", error="provider unavailable")

    store, service = seeded_service(summary_model=model)
    summary = service.summary("review-summary")
    assert summary.recommendation_from_llm is False
    assert "überarbeitet" in summary.recommendation
    assert store.caches.get_summary_text(summary.decision_set_hash, "summary-fake") is None


def test_budget_exhaustion_keeps_the_rule_based_summary() -> None:
    summary_module._failure_until.clear()

    def model(system: str, user: str) -> Completion:
        raise BudgetExceeded("spent")

    _store, service = seeded_service(summary_model=model)

    result = service.summary("review-summary")

    assert result.recommendation_from_llm is False
    assert "überarbeitet" in result.recommendation


def test_failed_summary_call_is_single_flight_and_negative_cached() -> None:
    summary_module._failure_until.clear()
    calls = 0

    def model(system: str, user: str) -> Completion:
        nonlocal calls
        calls += 1
        return Completion("", 0, 0, "summary-fake", error="offline")

    _store, service = seeded_service(summary_model=model)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: service.summary("review-summary"), range(4)))
    repeated = service.summary("review-summary")

    assert calls == 1
    assert all(not result.recommendation_from_llm for result in [*results, repeated])


def test_malformed_model_reply_is_rejected_and_valid_text_is_plain_and_capped() -> None:
    malformed = Completion('{"recommendation": "abgebrochen', 1, 1, "summary-fake")
    assert summary_module._model_text(malformed) is None

    long_markdown = "**Wichtig** [Link](https://example.test) <b>Text</b> " + "x" * 2000
    valid = Completion(
        text='{"recommendation": ' + json.dumps(long_markdown) + "}",
        input_tokens=1,
        output_tokens=1,
        model="summary-fake",
    )
    text = summary_module._model_text(valid)

    assert text is not None and len(text) == summary_module._RECOMMENDATION_MAX_CHARS
    assert "**" not in text and "https://" not in text and "<b>" not in text


def test_decision_hash_covers_a_non_risk_note_and_reviewer() -> None:
    store, service = seeded_service(decide_uncertain=True)
    before = service.summary("review-summary").decision_set_hash
    dismissed = store.decisions.for_review("review-summary")[3]
    store.decisions.put(
        dismissed.model_copy(update={"note": "anderer Hinweis", "decided_by": "Clara Neu"}),
        agent_version=Judge.agent_version,
    )

    after = service.summary("review-summary").decision_set_hash

    assert after != before

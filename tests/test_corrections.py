"""Correction publishing is opt-in, source-gated, idempotent, and offline-testable."""

from types import SimpleNamespace

import pytest
from core.hashing import content_hash

from app.domain import ClauseSpan, ClauseVerdict, Decision, Document, Review
from app.review.corrections import Corrections, CorrectionSettings, correction_id, evaluate


class FakeLangfuse:
    def __init__(self) -> None:
        self.datasets: list[dict] = []
        self.items: dict[str, dict] = {}

    def create_dataset(self, **kwargs) -> None:
        self.datasets.append(kwargs)

    def create_dataset_item(self, **kwargs) -> None:
        self.items[kwargs["id"]] = kwargs


def objects():
    text = "Die Haftung ist ausgeschlossen."
    clause = ClauseSpan(position=0, text=text, content_hash=content_hash(text))
    document = Document(
        document_id="doc",
        filename="public.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash=clause.content_hash,
    )
    review = Review(
        review_id="review",
        document_id="doc",
        agent_version="agent-1",
        scope=["liability"],
        status="finished",
    )
    verdict = ClauseVerdict(
        content_hash=clause.content_hash,
        agent_version=review.agent_version,
        verdict="potentially_void",
        legal_basis=["bgb-309-7"],
        confidence=0.9,
    )
    decision = Decision(
        review_id=review.review_id,
        position=0,
        action="override",
        corrected_verdict="valid",
        model_verdict=verdict.verdict,
        model_basis=verdict.legal_basis,
    )
    return document, review, clause, verdict, decision


def test_override_upserts_one_item_and_reoverride_updates_it() -> None:
    langfuse = FakeLangfuse()
    writer = Corrections(
        langfuse,
        CorrectionSettings(enabled=True, allowed_sources=frozenset({"pdf"})),
    )
    document, review, clause, verdict, decision = objects()
    writer.upsert(document, review, clause, verdict, decision)
    changed = decision.model_copy(
        update={"corrected_verdict": "uncertain", "corrected_basis": ["bgb-307"]}
    )
    writer.upsert(document, review, clause, verdict, changed)

    assert list(langfuse.items) == [correction_id(clause.content_hash, review.review_id)]
    item = next(iter(langfuse.items.values()))
    assert item["input"]["text"] == clause.text
    assert item["expected_output"] == {
        "model_verdict": "potentially_void",
        "corrected_verdict": "uncertain",
        "corrected_basis": ["bgb-307"],
    }
    assert item["metadata"]["agent_version"] == "agent-1"
    assert item["metadata"]["review_link"] == "/reviews/review"


@pytest.mark.parametrize(
    "settings",
    [
        CorrectionSettings(enabled=False, allowed_sources=frozenset({"pdf"})),
        CorrectionSettings(enabled=True, allowed_sources=frozenset({"agb_de"})),
    ],
)
def test_disabled_or_disallowed_source_is_noop(settings) -> None:
    langfuse = FakeLangfuse()
    Corrections(langfuse, settings).upsert(*objects())
    assert langfuse.items == {}


def test_evaluation_refuses_without_explicit_approval(monkeypatch) -> None:
    called = SimpleNamespace(value=False)
    monkeypatch.setattr(
        "core.langfuse_client.client",
        lambda: setattr(called, "value", True),
    )
    with pytest.raises(SystemExit, match="--yes"):
        evaluate(approved=False)
    assert called.value is False

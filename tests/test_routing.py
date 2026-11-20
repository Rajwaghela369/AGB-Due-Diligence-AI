from core.llm import Answer, DecisionResult
from core.routing import classify_topic, classify_topics


def result(topic: str) -> DecisionResult:
    return DecisionResult(
        answers={"topic": Answer(value=topic, confidence=0.9)},
        input_tokens=10,
        output_tokens=0,
        model="gemini-decision-1",
    )


def test_classify_topic_uses_the_decision_choice(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.routing.decide",
        lambda text, questions, model, **kwargs: (
            calls.append((text, questions, model)) or result("warranty")
        ),
    )

    assert classify_topic("Mängelansprüche verjähren.") == "warranty"
    assert calls[0][1]["topic"]["type"] == "choice"
    assert calls[0][2] == {"provider": "gemini", "name": "gemini-decision-1"}


def test_classify_topics_preserves_order_and_empty_input(monkeypatch):
    monkeypatch.setattr(
        "core.routing.decide",
        lambda text, questions, model, **kwargs: result(
            "liability" if "Haftung" in text else "other"
        ),
    )

    assert classify_topics(["Haftung A", "Gerichtsstand", "Haftung B"]) == [
        "liability",
        "other",
        "liability",
    ]
    assert classify_topics([]) == []

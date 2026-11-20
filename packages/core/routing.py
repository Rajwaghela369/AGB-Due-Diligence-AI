"""Context-aware topic routing through the Gemini Decisions API."""

from typing import Any

from core.budget import RequestBudget
from core.llm import DecisionResult, decide
from core.logging import get_logger

log = get_logger("core.routing")
LABELS = ("liability", "warranty", "other")
ROUTING_QUESTIONS = {
    "topic": {
        "type": "choice",
        "instructions": (
            "Ordne die Klausel nach ihrem tatsächlichen Regelungsgegenstand genau einem "
            "Thema zu. Wähle 'other', wenn kein Thema eindeutig passt."
        ),
        "criteria": {
            "liability": "Haftung, Haftungsausschluss, Schadensersatz oder Verantwortlichkeit",
            "warranty": "Gewährleistung, Mängelrechte, Garantie, Reparatur oder Ersatzlieferung",
            "other": "Ein anderes Thema oder keine eindeutige Zuordnung",
        },
    }
}


def _topic(result: DecisionResult) -> str:
    if result.error:
        raise RuntimeError(f"topic decision failed: {result.error}")
    answer = result.answers.get("topic")
    if answer is None or answer.value not in LABELS:
        value = answer.value if answer else None
        raise RuntimeError(f"decision router returned unknown topic {value!r}")
    return str(answer.value)


def classify_topics(
    texts: list[str],
    *,
    model: str = "gemini-decision-1",
    budget: RequestBudget | None = None,
    client: Any = None,
) -> list[str]:
    """Classify each clause with one typed Gemini choice question."""
    if not texts:
        return []
    topics = [
        _topic(
            decide(
                text,
                ROUTING_QUESTIONS,
                {"provider": "gemini", "name": model},
                budget=budget,
                client=client,
            )
        )
        for text in texts
    ]
    log.info("classified %d clauses", len(texts))
    return topics


def classify_topic(
    text: str,
    *,
    model: str = "gemini-decision-1",
    budget: RequestBudget | None = None,
    client: Any = None,
) -> str:
    return classify_topics([text], model=model, budget=budget, client=client)[0]

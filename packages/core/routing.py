"""Low-cost topic routing through Jina's zero-shot classifier.

Phase 9 (9.A): every clause is masked (`core.masking.boundary()`) before it is sent; the
topic does not depend on whose name is in the clause.
"""

from typing import Any

import requests

from core.logging import get_logger
from core.masking import boundary
from core.settings import load_settings

log = get_logger("core.routing")
ENDPOINT = "https://api.jina.ai/v1/classify"
LABELS = ("liability", "warranty", "other")
BATCH_SIZE = 64  # inputs per classify request


def classify_topics(texts: list[str], *, model: str = "jina-embeddings-v3") -> list[str]:
    """One topic per text, in order; a request per `BATCH_SIZE` texts rather than per text."""
    if not texts:
        return []
    settings = load_settings()
    if not settings.jina_api_key:
        raise RuntimeError("JINA_API_KEY is not set; copy .env.example to .env")
    topics: list[str] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = [boundary().outbound(text) for text in texts[start : start + BATCH_SIZE]]
        boundary().require_masked(batch)
        response = requests.post(
            ENDPOINT,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={"model": model, "labels": list(LABELS), "input": batch},
            timeout=60,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        data = payload.get("data", [])
        if len(data) != len(batch):
            raise RuntimeError(f"Jina classifier returned {len(data)} results for {len(batch)}")
        for item in sorted(data, key=lambda item: item.get("index", 0)):
            topics.append(_top_label(item.get("predictions", [])))
    log.info("classified %d clauses", len(texts))
    return topics


def classify_topic(text: str, *, model: str = "jina-embeddings-v3") -> str:
    return classify_topics([text], model=model)[0]


def _top_label(predictions: list[dict[str, Any]]) -> str:
    if not predictions:
        raise RuntimeError("Jina classifier returned no predictions")
    topic = max(predictions, key=lambda item: float(item["score"]))["label"]
    if topic not in LABELS:
        raise RuntimeError(f"Jina classifier returned unknown topic {topic!r}")
    return str(topic)

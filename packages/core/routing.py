"""Low-cost topic routing through Jina's zero-shot classifier."""

from typing import Any

import requests

from core.logging import get_logger
from core.settings import load_settings

log = get_logger("core.routing")
ENDPOINT = "https://api.jina.ai/v1/classify"
LABELS = ("liability", "warranty", "other")


def classify_topic(text: str, *, model: str = "jina-embeddings-v3") -> str:
    settings = load_settings()
    if not settings.jina_api_key:
        raise RuntimeError("JINA_API_KEY is not set; copy .env.example to .env")
    response = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {settings.jina_api_key}",
            "Content-Type": "application/json",
        },
        json={"model": model, "labels": list(LABELS), "input": [text]},
        timeout=60,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    predictions = payload.get("data", [{}])[0].get("predictions", [])
    if not predictions:
        raise RuntimeError("Jina classifier returned no predictions")
    topic = max(predictions, key=lambda item: float(item["score"]))["label"]
    if topic not in LABELS:
        raise RuntimeError(f"Jina classifier returned unknown topic {topic!r}")
    log.info("classified clause as %s", topic)
    return str(topic)

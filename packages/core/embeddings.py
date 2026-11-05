"""Embedding access through the Jina API. Nothing runs locally."""

from dataclasses import dataclass

import requests

from core.logging import get_logger
from core.settings import load_settings

log = get_logger("core.embeddings")

ENDPOINT = "https://api.jina.ai/v1/embeddings"
TIMEOUT_SECONDS = 120

# Jina serves task-specific LoRA adapters, so indexing and searching must use
# different ones. Embedding a query with the passage adapter degrades retrieval
# without raising anything.
TASK_PASSAGE = "retrieval.passage"
TASK_QUERY = "retrieval.query"


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    tokens: int


def embed_texts(
    texts: list[str],
    model: str,
    dimension: int,
    task: str,
    batch_size: int = 64,
    normalized: bool = True,
) -> EmbeddingBatch:
    api_key = load_settings().jina_api_key
    if not api_key:
        raise RuntimeError("JINA_API_KEY is not set; copy .env.example to .env")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    vectors: list[list[float]] = []
    tokens = 0

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        response = requests.post(
            ENDPOINT,
            headers=headers,
            json={
                "model": model,
                "task": task,
                "dimensions": dimension,
                "normalized": normalized,
                "input": batch,
            },
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        # The API may return items out of order, so place them by index.
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        vectors.extend(item["embedding"] for item in ordered)
        tokens += payload.get("usage", {}).get("total_tokens", 0)
        log.info("embedded %d/%d (%s)", len(vectors), len(texts), task)

    if len(vectors) != len(texts):
        raise RuntimeError(f"asked for {len(texts)} embeddings, received {len(vectors)}")
    if vectors and len(vectors[0]) != dimension:
        raise RuntimeError(f"expected {dimension} dimensions, received {len(vectors[0])}")

    return EmbeddingBatch(vectors=vectors, tokens=tokens)

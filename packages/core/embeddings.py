"""Embedding access through the Jina API. Nothing runs locally."""

from dataclasses import dataclass

import requests

from core.hashing import content_hash
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
    cache: object | None = None,
) -> EmbeddingBatch:
    """Embed text, reusing vectors whose content and model have not changed."""
    cached: dict[int, list[float]] = {}
    missing: list[tuple[int, str, str]] = []
    if cache is not None:
        for index, text in enumerate(texts):
            digest = content_hash(text)
            vector = cache.get_embedding(digest, model)
            if vector is None:
                missing.append((index, text, digest))
            else:
                cached[index] = vector
    else:
        missing = [(index, text, content_hash(text)) for index, text in enumerate(texts)]

    if not missing:
        return EmbeddingBatch(vectors=[cached[index] for index in range(len(texts))], tokens=0)

    api_key = load_settings().jina_api_key
    if not api_key:
        raise RuntimeError("JINA_API_KEY is not set; copy .env.example to .env")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    vectors: list[list[float]] = []
    tokens = 0

    for start in range(0, len(missing), batch_size):
        batch_entries = missing[start : start + batch_size]
        batch = [entry[1] for entry in batch_entries]
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
        for (index, _, digest), item in zip(batch_entries, ordered, strict=True):
            vector = item["embedding"]
            cached[index] = vector
            if cache is not None:
                cache.put_embedding(digest, model, vector)
        tokens += payload.get("usage", {}).get("total_tokens", 0)
        log.info("embedded %d/%d (%s)", len(cached), len(texts), task)

    vectors = [cached[index] for index in range(len(texts))]
    if vectors and len(vectors[0]) != dimension:
        raise RuntimeError(f"expected {dimension} dimensions, received {len(vectors[0])}")

    return EmbeddingBatch(vectors=vectors, tokens=tokens)

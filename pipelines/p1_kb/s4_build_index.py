"""1.4 Embed and index: chunks into a versioned Qdrant collection."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import qdrant as store
from core.logging import get_logger
from core.schemas import LawChunk, StoreManifest
from core.settings import load_settings
from openai import OpenAI
from qdrant_client.models import PointStruct

log = get_logger("p1.index")


@dataclass(frozen=True)
class IndexResult:
    collection: str
    points: int
    tokens: int
    manifest_path: Path
    manifest: StoreManifest


def embed(
    texts: list[str], model: str, dimension: int, batch_size: int
) -> tuple[list[list[float]], int]:
    """Embed through the API in batches; nothing runs locally."""
    client = OpenAI(api_key=load_settings().openai_api_key)
    vectors: list[list[float]] = []
    tokens = 0

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        response = client.embeddings.create(model=model, input=batch, dimensions=dimension)
        vectors.extend(item.embedding for item in response.data)
        tokens += response.usage.total_tokens
        log.info("embedded %d/%d", len(vectors), len(texts))

    return vectors, tokens


def _payload(chunk: LawChunk) -> dict[str, Any]:
    # embed_text is the input to the model, not something a reader needs.
    payload = chunk.model_dump(exclude={"embed_text"})
    payload["kind"] = store.KIND_CHUNK
    return payload


def build_index(
    chunks_path: Path,
    kb: dict[str, Any],
    config_hash: str,
    manifest_dir: Path,
) -> IndexResult:
    chunks = [
        LawChunk(**json.loads(line))
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
    ]
    embedding = kb["embedding"]
    collection = kb["store"]["collection"]

    vectors, tokens = embed(
        [chunk.embed_text for chunk in chunks],
        model=embedding["model"],
        dimension=embedding["dimension"],
        batch_size=embedding["batch_size"],
    )

    qdrant = store.client()
    store.recreate_collection(
        qdrant,
        collection=collection,
        dimension=embedding["dimension"],
        distance=kb["store"]["distance"],
        payload_indexes=tuple(kb["store"]["payload_indexes"]),
    )
    qdrant.upsert(
        collection,
        points=[
            PointStruct(id=store.point_id(chunk.chunk_id), vector=vector, payload=_payload(chunk))
            for chunk, vector in zip(chunks, vectors, strict=True)
        ],
    )

    manifest = StoreManifest(
        collection=collection,
        embedding_model=embedding["model"],
        embedding_dimension=embedding["dimension"],
        source_dump=kb["source"]["config"],
        chunk_count=len(chunks),
        config_hash=config_hash,
        built_at=datetime.now(UTC).isoformat(),
    )
    store.write_manifest(qdrant, collection, manifest)

    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    # Counts the manifest point, which is excluded from search by its kind.
    indexed = qdrant.count(collection, exact=True).count
    if indexed != len(chunks) + 1:
        raise ValueError(f"expected {len(chunks) + 1} points in {collection}, found {indexed}")

    log.info("indexed %d chunks into %s (%d tokens)", len(chunks), collection, tokens)
    return IndexResult(
        collection=collection,
        points=indexed,
        tokens=tokens,
        manifest_path=manifest_path,
        manifest=manifest,
    )

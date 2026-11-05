"""1.4 Embed and index: chunks into a versioned Qdrant collection."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import qdrant as store
from core.embeddings import embed_texts
from core.logging import get_logger
from core.schemas import LawChunk, StoreManifest
from qdrant_client.models import PointStruct

log = get_logger("p1.index")


@dataclass(frozen=True)
class IndexResult:
    collection: str
    points: int
    tokens: int
    manifest_path: Path
    manifest: StoreManifest


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

    embedded = embed_texts(
        [chunk.embed_text for chunk in chunks],
        model=embedding["model"],
        dimension=embedding["dimension"],
        task=embedding["task_passage"],
        batch_size=embedding["batch_size"],
        normalized=embedding["normalize"],
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
            for chunk, vector in zip(chunks, embedded.vectors, strict=True)
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

    log.info("indexed %d chunks into %s (%d tokens)", len(chunks), collection, embedded.tokens)
    return IndexResult(
        collection=collection,
        points=indexed,
        tokens=embedded.tokens,
        manifest_path=manifest_path,
        manifest=manifest,
    )

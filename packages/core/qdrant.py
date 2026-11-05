"""Qdrant access shared by the pipelines (which write) and the app (which reads)."""

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
    Distance,
    PointStruct,
    VectorParams,
)

from core.schemas import StoreManifest
from core.settings import load_settings

# Qdrant point ids must be int or UUID, so chunk ids are hashed into a stable
# namespace and kept verbatim in the payload.
_ID_NAMESPACE = uuid.UUID("6f1a7c2e-0000-4000-8000-000000000001")

# The manifest rides along as a reserved point so it travels with the remote
# collection; `kind` keeps it out of every search.
MANIFEST_POINT_ID = str(uuid.uuid5(_ID_NAMESPACE, "__manifest__"))
KIND_CHUNK = "chunk"
KIND_MANIFEST = "manifest"


def client() -> QdrantClient:
    settings = load_settings()
    if not settings.qdrant_url:
        raise RuntimeError("QDRANT_URL is not set; copy .env.example to .env")
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, chunk_id))


def recreate_collection(
    qdrant: QdrantClient,
    collection: str,
    dimension: int,
    distance: str = "Cosine",
    payload_indexes: tuple[str, ...] = (),
) -> None:
    """Build a versioned collection from scratch; rebuilds must be reproducible."""
    if qdrant.collection_exists(collection):
        qdrant.delete_collection(collection)

    qdrant.create_collection(
        collection,
        vectors_config=VectorParams(size=dimension, distance=Distance[distance.upper()]),
    )
    for field in payload_indexes:
        qdrant.create_payload_index(collection, field_name=field, field_schema="keyword")


def write_manifest(qdrant: QdrantClient, collection: str, manifest: StoreManifest) -> None:
    dimension = qdrant.get_collection(collection).config.params.vectors.size
    qdrant.upsert(
        collection,
        points=[
            PointStruct(
                id=MANIFEST_POINT_ID,
                vector=[0.0] * dimension,
                payload={"kind": KIND_MANIFEST, **manifest.model_dump()},
            )
        ],
    )


def read_manifest(qdrant: QdrantClient, collection: str) -> StoreManifest | None:
    found = qdrant.retrieve(collection, ids=[MANIFEST_POINT_ID], with_payload=True)
    if not found:
        return None
    payload = dict(found[0].payload or {})
    payload.pop("kind", None)
    return StoreManifest(**payload)


def point_alias(qdrant: QdrantClient, alias: str, collection: str) -> None:
    """Publish a validated collection. The app only ever reads the alias.

    Delete-then-create in one operation list, so readers never see it missing.
    Deleting an alias that does not exist is a no-op, which makes the first
    publish identical to every later one.
    """
    qdrant.update_collection_aliases(
        change_aliases_operations=[
            DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias)),
            CreateAliasOperation(
                create_alias=CreateAlias(collection_name=collection, alias_name=alias)
            ),
        ]
    )

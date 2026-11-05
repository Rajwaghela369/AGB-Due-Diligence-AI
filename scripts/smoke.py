"""Prove every external dependency works, independently, before real code runs.

Each check reports on its own so one failure cannot mask the rest.
Writes reports/p0_smoke.json.

Run: make smoke
"""

import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

from core.logging import get_logger
from core.paths import REPORTS
from core.settings import load_settings

log = get_logger("smoke")
GERMAN_SENTENCE = (
    "Unsere Haftung ist ausgeschlossen und auf das gesetzlich zulässige Maß beschränkt."
)


def check_dataset() -> dict[str, Any]:
    """The laws dump, which the knowledge base is built from."""
    from datasets import get_dataset_config_names
    from huggingface_hub import HfApi

    dataset = "openlegaldata/laws-germany"
    token = load_settings().hf_token or None
    return {
        "dataset": dataset,
        "revision": HfApi(token=token).dataset_info(dataset).sha,
        "configs": get_dataset_config_names(dataset, token=token),
    }


def check_embedding() -> dict[str, Any]:
    from core.embeddings import TASK_PASSAGE, embed_texts

    model = "jina-embeddings-v5-text-small"
    batch = embed_texts([GERMAN_SENTENCE], model=model, dimension=1024, task=TASK_PASSAGE)
    return {"model": model, "dimension": len(batch.vectors[0]), "tokens": batch.tokens}


def check_vector_store() -> dict[str, Any]:
    """Qdrant roundtrip, including the UUID5 point-id strategy the index relies on."""
    from core import qdrant as store
    from qdrant_client.models import Distance, PointStruct, VectorParams

    qdrant = store.client()
    name = "smoke"
    if not qdrant.collection_exists(name):
        qdrant.create_collection(
            name, vectors_config=VectorParams(size=4, distance=Distance.COSINE)
        )
    chunk_id = "bgb-309-7-a"
    qdrant.upsert(
        name,
        points=[
            PointStruct(
                id=store.point_id(chunk_id),
                vector=[0.1, 0.2, 0.3, 0.4],
                payload={"chunk_id": chunk_id},
            )
        ],
    )
    retrieved = qdrant.retrieve(name, ids=[store.point_id(chunk_id)], with_payload=True)
    qdrant.delete_collection(name)
    return {"roundtrip_ok": retrieved[0].payload["chunk_id"] == chunk_id}


def check_database() -> dict[str, Any]:
    from core.db import ping

    return {"server": ping().split(" on ")[0]}


def check_prefect() -> dict[str, Any]:
    """Confirm the local client is attached to a Prefect Cloud workspace."""
    import asyncio

    from prefect.client.orchestration import get_client

    async def _ping() -> dict[str, Any]:
        async with get_client() as client:
            await client.api_healthcheck()
            return {"api_url": str(client.api_url)}

    return asyncio.run(_ping())


def check_langfuse() -> dict[str, Any]:
    from core.langfuse_client import client

    return {"host": load_settings().langfuse_host, "authenticated": client().auth_check()}


CHECKS = {
    "dataset": check_dataset,
    "embedding": check_embedding,
    "vector_store": check_vector_store,
    "database": check_database,
    "prefect": check_prefect,
    "langfuse": check_langfuse,
}


def main() -> int:
    results: dict[str, Any] = {}
    for name, check in CHECKS.items():
        started = time.perf_counter()
        try:
            detail = check()
            results[name] = {
                "status": "pass",
                "seconds": round(time.perf_counter() - started, 2),
                **detail,
            }
            log.info("%-14s pass", name)
        except Exception as exc:  # noqa: BLE001 - one failure must not hide the others
            results[name] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
            log.error("%-14s FAIL  %s", name, exc)

    failed = [name for name, result in results.items() if result["status"] == "fail"]
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "p0_smoke.json").write_text(
        json.dumps(
            {"ran_at": datetime.now(UTC).isoformat(), "failed": failed, "checks": results},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log.info("report written to reports/p0_smoke.json")
    if failed:
        log.error("not met: %s", ", ".join(failed))
        return 1
    log.info("all dependencies proven")
    return 0


if __name__ == "__main__":
    sys.exit(main())

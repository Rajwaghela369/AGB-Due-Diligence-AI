"""Phase 0 gate: prove every external dependency works before any real code exists.

Six checks, each independent, each reporting pass/fail with the detail worth
keeping. Writes reports/p0_smoke.json.

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
GERMAN_SENTENCE = "Unsere Haftung ist ausgeschlossen und auf das gesetzlich zulässige Maß beschränkt."


def check_dataset() -> dict[str, Any]:
    """Confirm the Open Legal Data field names, which the design docs flag as unverified."""
    from datasets import load_dataset

    ds = load_dataset("openlegaldata/laws-germany", split="train", streaming=True)
    record = next(iter(ds))
    return {"fields": sorted(record.keys()), "sample_keys_present": len(record) > 0}


def check_embedding() -> dict[str, Any]:
    from openai import OpenAI

    model = "text-embedding-3-large"
    client = OpenAI(api_key=load_settings().openai_api_key)
    vector = client.embeddings.create(model=model, input=[GERMAN_SENTENCE]).data[0].embedding
    return {"model": model, "dimension": len(vector)}


def check_vector_store() -> dict[str, Any]:
    """Qdrant Cloud roundtrip, including the UUID5 point-id strategy we rely on."""
    import uuid

    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    s = load_settings()
    client = QdrantClient(url=s.qdrant_url, api_key=s.qdrant_api_key)
    name = "smoke"
    if not client.collection_exists(name):
        client.create_collection(name, vectors_config=VectorParams(size=4, distance=Distance.COSINE))
    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "bgb-309-7-a"))
    client.upsert(
        name,
        points=[PointStruct(id=point_id, vector=[0.1, 0.2, 0.3, 0.4],
                            payload={"chunk_id": "bgb-309-7-a", "text": GERMAN_SENTENCE})],
    )
    got = client.retrieve(name, ids=[point_id], with_payload=True)
    client.delete_collection(name)
    return {"roundtrip_ok": got[0].payload["chunk_id"] == "bgb-309-7-a", "uuid5_ok": True}


def check_prefect() -> dict[str, Any]:
    """Confirm the local client is attached to a Prefect Cloud workspace."""
    import asyncio

    from prefect.client.orchestration import get_client

    async def _ping() -> dict[str, Any]:
        async with get_client() as client:
            healthy = await client.api_healthcheck()
            return {"api_url": str(client.api_url), "healthy": healthy is None}

    return asyncio.run(_ping())


def check_llm_structured_output() -> dict[str, Any]:
    """The judge node depends on reliable constrained JSON, in German. Verify it here."""
    from openai import OpenAI

    schema = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "enum": ["liability", "warranty", "other"]},
            "reason_de": {"type": "string"},
        },
        "required": ["topic", "reason_de"],
        "additionalProperties": False,
    }
    client = OpenAI(api_key=load_settings().openai_api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {"role": "system", "content": "Du bist ein deutscher Jurist. Antworte auf Deutsch."},
            {"role": "user", "content": f"Welches Thema hat diese AGB-Klausel?\n\n{GERMAN_SENTENCE}"},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "topic_check", "schema": schema, "strict": True},
        },
    )
    parsed = json.loads(response.choices[0].message.content)
    return {"model": "gpt-4o-mini", "parsed": parsed, "schema_respected": parsed["topic"] in {"liability", "warranty", "other"}}


def check_langfuse() -> dict[str, Any]:
    from langfuse import Langfuse

    s = load_settings()
    client = Langfuse(public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host)
    trace = client.trace(name="p0-smoke", input={"clause": GERMAN_SENTENCE})
    trace.span(name="smoke-span", output={"ok": True}).end()
    client.flush()
    return {"host": s.langfuse_host, "trace_id": trace.id}


CHECKS = {
    "dataset_fields": check_dataset,
    "embedding": check_embedding,
    "vector_store": check_vector_store,
    "prefect": check_prefect,
    "llm_structured_output": check_llm_structured_output,
    "langfuse": check_langfuse,
}


def main() -> int:
    results: dict[str, Any] = {}
    for name, fn in CHECKS.items():
        started = time.perf_counter()
        try:
            detail = fn()
            results[name] = {"status": "pass", "seconds": round(time.perf_counter() - started, 2), **detail}
            log.info("%-22s pass", name)
        except Exception as exc:  # noqa: BLE001 - one failing check must not hide the others
            results[name] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
            log.error("%-22s FAIL  %s", name, exc)

    failed = [n for n, r in results.items() if r["status"] == "fail"]
    report = {"ran_at": datetime.now(UTC).isoformat(), "failed": failed, "checks": results}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "p0_smoke.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("report written to reports/p0_smoke.json")
    if failed:
        log.error("Phase 0 gate NOT met: %s", ", ".join(failed))
        return 1
    log.info("Phase 0 gate met: all dependencies proven")
    return 0


if __name__ == "__main__":
    sys.exit(main())

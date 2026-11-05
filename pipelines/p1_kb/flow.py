"""P1 KB-INGEST: the BGB knowledge base, from HuggingFace to a published Qdrant alias."""

import argparse

from prefect import flow, get_run_logger, task
from prefect.artifacts import create_table_artifact

from core.config import load_yaml
from core.paths import CONFIGS

FULL_CONFIG = "dump-20260520"
SAMPLE_CONFIG = "dump-20260520-1k"

# Verified against the dump: book_code == "BGB" yields this many sections.
EXPECTED_BGB_ROWS = 2538


@task(retries=3, retry_delay_seconds=10)
def fetch_laws(dataset_config: str) -> int:
    """1.1 Stream the dump, keep the BGB rows, snapshot them as parquet."""
    get_run_logger().info("fetch_laws: not implemented (config=%s)", dataset_config)
    return 0


@task
def select_norms() -> int:
    """1.2 Resolve the whitelist against the snapshot. Fails on any missing section."""
    get_run_logger().info("select_norms: not implemented")
    return 0


@task
def chunk_norms() -> int:
    """1.3 Parse the content HTML into Absatz and item chunks."""
    get_run_logger().info("chunk_norms: not implemented")
    return 0


@task(retries=3, retry_delay_seconds=10)
def build_index() -> int:
    """1.4 Embed the chunks and upsert them into a versioned collection."""
    get_run_logger().info("build_index: not implemented")
    return 0


@task
def check_retrieval() -> dict[str, float]:
    """1.5 Score the gold queries. The gate that decides whether the alias moves."""
    get_run_logger().info("check_retrieval: not implemented")
    return {}


@flow(name="p1-kb-ingest")
def kb_ingest(dataset_config: str = FULL_CONFIG, publish: bool = True) -> None:
    log = get_run_logger()
    kb = load_yaml(CONFIGS / "kb.yaml")
    sampling = dataset_config != FULL_CONFIG

    log.info("config=%s sampling=%s collection=%s", dataset_config, sampling, kb["store"]["collection"])

    rows = fetch_laws(dataset_config)

    # A sample dump contains no BGB at all, so the row gate would fail by design.
    # Failing a deliberate plumbing run teaches people to ignore red runs.
    if sampling:
        log.warning("sample run: %d BGB rows (row gate skipped)", rows)
    elif rows != EXPECTED_BGB_ROWS:
        raise ValueError(f"expected {EXPECTED_BGB_ROWS} BGB rows, snapshot has {rows}")

    selected = select_norms()
    chunks = chunk_norms()
    indexed = build_index()
    scores = check_retrieval()

    create_table_artifact(
        key="p1-kb-ingest-summary",
        table=[
            {"step": "1.1 fetch", "result": rows},
            {"step": "1.2 select", "result": selected},
            {"step": "1.3 chunk", "result": chunks},
            {"step": "1.4 index", "result": indexed},
            {"step": "1.5 retrieval", "result": str(scores)},
        ],
        description="P1 knowledge base ingestion",
    )

    if not publish:
        log.info("publish disabled, alias left untouched")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=FULL_CONFIG, help=f"use {SAMPLE_CONFIG} for a plumbing run")
    ap.add_argument("--no-publish", action="store_true", help="never move the alias")
    args = ap.parse_args()
    kb_ingest(dataset_config=args.config, publish=not args.no_publish)

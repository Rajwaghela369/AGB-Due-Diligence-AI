"""P1 KB-INGEST: the BGB knowledge base, from HuggingFace to a published Qdrant alias."""

import argparse
from pathlib import Path

from core.config import load_yaml
from core.paths import CONFIGS, INTERIM, PROCESSED, RAW, REPORTS
from core.settings import load_settings
from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact

from pipelines.p1_kb.s1_fetch_laws import FetchResult, fetch_bgb_snapshot, resolve_revision
from pipelines.p1_kb.s2_select_norms import SelectResult
from pipelines.p1_kb.s2_select_norms import select_norms as run_select
from pipelines.p1_kb.s3_chunk_norms import ChunkResult
from pipelines.p1_kb.s3_chunk_norms import chunk_norms as run_chunk

FULL_CONFIG = "dump-20260520"
SAMPLE_CONFIG = "dump-20260520-1k"

# Verified against the full dump: book_code == "BGB" yields this many sections.
EXPECTED_BGB_ROWS = 2538


@task(retries=3, retry_delay_seconds=10)
def fetch_laws(source: dict, dataset_config: str) -> FetchResult:
    """1.1 Stream the dump, keep the BGB rows, snapshot them as parquet."""
    log = get_run_logger()
    token = load_settings().hf_token or None

    revision = resolve_revision(source["dataset"], source.get("revision"), token)
    result = fetch_bgb_snapshot(
        dataset=source["dataset"],
        dataset_config=dataset_config,
        revision=revision,
        out_dir=RAW / "laws",
        token=token,
    )
    log.info(
        "kept %d of %d rows at revision %s", result.rows_kept, result.rows_scanned, revision[:12]
    )
    return result


@task
def select_norms(snapshot: Path, source: dict, revision: str) -> SelectResult:
    """1.2 Resolve the whitelist against the snapshot. Fails on any missing section."""
    whitelist = load_yaml(CONFIGS / "kb_paragraphs.yaml")["paragraphs"]
    result = run_select(
        snapshot=snapshot,
        whitelist=whitelist,
        source={**source, "revision": revision},
        out_dir=INTERIM,
    )
    get_run_logger().info(
        "resolved %d/%d sections -> %s", result.selected, len(whitelist), result.path.name
    )
    return result


@task
def chunk_norms(selected: SelectResult) -> ChunkResult:
    """1.3 Parse the content markup into Absatz and item chunks."""
    result = run_chunk(selected.path, out_dir=PROCESSED, report_dir=REPORTS)
    get_run_logger().info("produced %d chunks -> %s", len(result.chunks), result.path.name)

    # The manual review in task 1.3g happens on this artifact.
    create_markdown_artifact(
        key="p1-bgb-chunks",
        markdown=result.report_path.read_text(encoding="utf-8"),
        description=f"{len(result.chunks)} BGB chunks for review",
    )
    return result


@task(retries=3, retry_delay_seconds=10)
def build_index(chunks: ChunkResult) -> int:
    """1.4 Embed the chunks via the OpenAI API and upsert them into a versioned collection."""
    get_run_logger().info("build_index: not implemented")
    return 0


@task
def check_retrieval(indexed: int) -> dict[str, float]:
    """1.5 Score the gold queries. The gate that decides whether the alias moves."""
    get_run_logger().info("check_retrieval: not implemented")
    return {}


@flow(name="p1-kb-ingest")
def kb_ingest(dataset_config: str = FULL_CONFIG, publish: bool = True) -> None:
    log = get_run_logger()
    kb = load_yaml(CONFIGS / "kb.yaml")
    sampling = dataset_config != FULL_CONFIG

    log.info(
        "config=%s sampling=%s collection=%s", dataset_config, sampling, kb["store"]["collection"]
    )

    fetched = fetch_laws(kb["source"], dataset_config)

    # A sample dump holds an arbitrary subset of sections, so neither the row
    # count nor the whitelist can resolve. Failing a deliberate plumbing run
    # would just teach people to ignore red runs.
    if sampling:
        log.warning("sample run: %d BGB rows (row gate skipped)", fetched.rows_kept)
    elif fetched.rows_kept != EXPECTED_BGB_ROWS:
        raise ValueError(f"expected {EXPECTED_BGB_ROWS} BGB rows, snapshot has {fetched.rows_kept}")

    if sampling:
        log.warning("sample run: stopping after the snapshot, the whitelist cannot resolve")
        return

    selected = select_norms(fetched.path, kb["source"], fetched.revision)
    chunks = chunk_norms(selected)
    indexed = build_index(chunks)
    scores = check_retrieval(indexed)

    create_table_artifact(
        key="p1-kb-ingest-summary",
        table=[
            {"step": "1.1 fetch", "result": f"{fetched.rows_kept} rows -> {fetched.path.name}"},
            {
                "step": "1.2 select",
                "result": f"{selected.selected} paragraphs "
                f"({selected.item_level} item, {selected.absatz_level} Absatz)",
            },
            {"step": "1.3 chunk", "result": f"{len(chunks.chunks)} chunks"},
            {"step": "1.4 index", "result": str(indexed)},
            {"step": "1.5 retrieval", "result": str(scores)},
        ],
        description=f"P1 knowledge base ingestion · {dataset_config} @ {fetched.revision[:12]}",
    )

    if not publish:
        log.info("publish disabled, alias left untouched")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=FULL_CONFIG, help=f"use {SAMPLE_CONFIG} for a plumbing run")
    ap.add_argument("--no-publish", action="store_true", help="never move the alias")
    args = ap.parse_args()
    kb_ingest(dataset_config=args.config, publish=not args.no_publish)

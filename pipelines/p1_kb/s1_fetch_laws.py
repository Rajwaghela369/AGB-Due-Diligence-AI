"""1.1 Acquire: snapshot the BGB rows of the Open Legal Data dump as parquet."""

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from core.logging import get_logger
from datasets import load_dataset
from huggingface_hub import HfApi

log = get_logger("p1.fetch")

BOOK_CODE = "BGB"

# The raw layer stays faithful to the source: we subset rows, never columns.
SNAPSHOT_COLUMNS = (
    "id",
    "doknr",
    "book",
    "book_code",
    "book_slug",
    "section",
    "slug",
    "title",
    "content",
    "markdown_content",
    "amtabk",
    "kurzue",
    "order",
    "created_date",
    "updated_date",
)


@dataclass(frozen=True)
class FetchResult:
    path: Path
    revision: str
    dataset_config: str
    rows_scanned: int
    rows_kept: int


def resolve_revision(dataset: str, pinned: str | None, token: str | None) -> str:
    """Pinned lineage: the dump is only reproducible if the revision is fixed."""
    upstream = HfApi(token=token).dataset_info(dataset).sha
    if pinned and pinned != upstream:
        log.warning("config pins %s but %s is at %s", pinned[:12], dataset, upstream[:12])
    return pinned or upstream


def fetch_bgb_snapshot(
    dataset: str,
    dataset_config: str,
    revision: str,
    out_dir: Path,
    token: str | None = None,
    progress_every: int = 40_000,
) -> FetchResult:
    rows = []
    scanned = 0

    stream = load_dataset(
        dataset, dataset_config, split="train", streaming=True, revision=revision, token=token
    )
    for scanned, record in enumerate(stream, start=1):
        if record.get("book_code") == BOOK_CODE:
            rows.append({column: record.get(column) for column in SNAPSHOT_COLUMNS})
        if scanned % progress_every == 0:
            log.info("scanned %d rows, kept %d", scanned, len(rows))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"bgb_{dataset_config}_{revision[:8]}.parquet"

    # Explicit string schema: an all-null column would otherwise be typed as
    # null and break a later concat or read.
    schema = pa.schema([(column, pa.string()) for column in SNAPSHOT_COLUMNS])
    table = pa.Table.from_pylist(
        [{k: None if v is None else str(v) for k, v in row.items()} for row in rows],
        schema=schema,
    )
    pq.write_table(table, path, compression="zstd")

    log.info("wrote %d %s rows to %s", len(rows), BOOK_CODE, path.name)
    return FetchResult(
        path=path,
        revision=revision,
        dataset_config=dataset_config,
        rows_scanned=scanned,
        rows_kept=len(rows),
    )

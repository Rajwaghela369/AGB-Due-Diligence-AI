"""1.2 Select: resolve the paragraph whitelist against the snapshot."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from core.logging import get_logger

log = get_logger("p1.select")


class UnresolvedParagraphs(RuntimeError):
    """A whitelisted section is absent from the snapshot.

    Hard failure on purpose: a silently missing § 309 would surface much later
    as bad verdicts, and the debugging would start in the wrong place.
    """


@dataclass(frozen=True)
class SelectResult:
    path: Path
    selected: int
    item_level: int
    absatz_level: int


def select_norms(
    snapshot: Path,
    whitelist: list[dict[str, Any]],
    source: dict[str, Any],
    out_dir: Path,
) -> SelectResult:
    table = pq.read_table(
        snapshot,
        columns=["section", "title", "content", "markdown_content", "doknr", "updated_date"],
    )
    by_section = {row["section"]: row for row in table.to_pylist()}

    unresolved = [entry["ref"] for entry in whitelist if entry["ref"] not in by_section]
    if unresolved:
        raise UnresolvedParagraphs(
            f"{len(unresolved)} of {len(whitelist)} whitelisted sections absent from "
            f"{snapshot.name}: {', '.join(unresolved)}"
        )

    records = []
    for entry in whitelist:
        row = by_section[entry["ref"]]
        records.append(
            {
                "section": entry["ref"],
                "title": row["title"],
                "content": row["content"],
                "markdown_content": row["markdown_content"],
                "doknr": row["doknr"],
                "updated_date": row["updated_date"],
                # Chunking instructions travel with the norm, so s3 needs no config.
                "level": entry["level"],
                "topics": entry["topics"],
                "item_topics": entry.get("items", {}),
                "source": source,
            }
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "bgb_selected.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    item_level = sum(1 for r in records if r["level"] == "item")
    log.info(
        "selected %d paragraphs (%d item-level, %d Absatz-level)",
        len(records),
        item_level,
        len(records) - item_level,
    )
    return SelectResult(
        path=path,
        selected=len(records),
        item_level=item_level,
        absatz_level=len(records) - item_level,
    )

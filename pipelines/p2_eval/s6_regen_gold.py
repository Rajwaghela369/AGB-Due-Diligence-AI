"""2.6 Regenerate the retrieval answer key from real clause text, and re-score it.

Phase 1's gate scored 0.933 against 30 clauses Claude wrote and labelled - the
index proved structurally sound against a test it had helped define. This step
replaces the questions, not the index: nothing is written to Qdrant, which has
held real BGB text since Phase 1.

The clause text comes from the dev-half parquet rather than the mapping file, so
a hand-edited answer key cannot drift from the corpus. Only the chunk ids and
the reasoning are hand-authored, in configs/gold_map.yaml.

The design doc proposed a `subtopic -> chunk_id` mapping instead. Only 16 of 523
in-scope clauses carry a subtopic, 14 in the dev half, 8 of those sharing one
value - about five effective queries. The void annotation is the denser expert
signal, so the key is built from the 31 void dev clauses.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from core.logging import get_logger

from pipelines.p1_kb.s5_check_retrieval import RetrievalResult
from pipelines.p1_kb.s5_check_retrieval import check_retrieval as run_check

log = get_logger("p2.gold")

RECHECK_REPORT = "p2_retrieval_recheck.json"
IN_SCOPE_TOPICS = ("liability", "warranty")


class GoldMapError(RuntimeError):
    """The mapping and the data disagree.

    Hard failure for the same reason P1 fails on an unresolved paragraph: a
    mapping that silently points at a clause which is not void, or at a chunk
    that is not in the index, produces a retrieval score measuring nothing.
    """


@dataclass(frozen=True)
class GoldResult:
    gold_path: Path
    queries: int
    low_confidence: list[str]
    retrieval: RetrievalResult


def router_topic(topics: list[str], override: str | None) -> str:
    """The topic the router would pick, used as the search filter.

    Eight in-scope clauses carry two topics, so the mapping may override.
    """
    if override:
        return override
    for topic in IN_SCOPE_TOPICS:
        if topic in topics:
            return topic
    raise GoldMapError(f"no in-scope topic among {topics}")


def build_gold(
    mapping: list[dict[str, Any]],
    dev: pd.DataFrame,
    chunk_ids: set[str],
) -> list[dict[str, Any]]:
    """Join the hand-authored mapping onto the real clause text, validating both ends."""
    by_clause = dev.set_index("clause_id")
    rows = []

    for entry in mapping:
        clause_id = entry["clause_id"]
        if clause_id not in by_clause.index:
            raise GoldMapError(f"{clause_id} is not in the dev half")

        clause = by_clause.loc[clause_id]
        if not bool(clause.label_void):
            raise GoldMapError(f"{clause_id} is mapped but is not labelled void")

        referenced = [entry["primary_chunk_id"], *entry.get("also_relevant", [])]
        missing = [chunk for chunk in referenced if chunk not in chunk_ids]
        if missing:
            raise GoldMapError(f"{clause_id} references chunk ids absent from the index: {missing}")

        rows.append(
            {
                "query_id": clause_id,
                "topic": router_topic(list(clause.topics), entry.get("topic")),
                "primary_chunk_id": entry["primary_chunk_id"],
                "also_relevant": entry.get("also_relevant", []),
                # Verbatim from the corpus, placeholders and all. This is the
                # whole point: the query is a real clause, not a tidy paraphrase.
                "query": clause.text,
                "confidence": entry.get("confidence", "normal"),
            }
        )

    mapped = {entry["clause_id"] for entry in mapping}
    unmapped = set(by_clause.index[by_clause.label_void]) - mapped
    if unmapped:
        raise GoldMapError(
            f"{len(unmapped)} void dev clauses have no mapping: {sorted(unmapped)}. "
            "Every void clause must be mapped or the key is silently partial."
        )

    return rows


def write_gold(rows: list[dict[str, Any]], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agb_queries.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def regen_gold(
    mapping: list[dict[str, Any]],
    dev_path: Path,
    chunks_path: Path,
    kb: dict[str, Any],
    gold_dir: Path,
    report_dir: Path,
) -> GoldResult:
    dev = pd.read_parquet(dev_path)
    chunk_ids = {
        json.loads(line)["chunk_id"]
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
    }

    rows = build_gold(mapping, dev, chunk_ids)
    gold_path = write_gold(rows, gold_dir)
    low_confidence = [row["query_id"] for row in rows if row["confidence"] == "low"]

    log.info(
        "wrote %d real-text queries to %s (%d flagged low confidence)",
        len(rows),
        gold_path.name,
        len(low_confidence),
    )

    retrieval = run_check(gold_path, kb=kb, report_dir=report_dir, report_name=RECHECK_REPORT)
    return GoldResult(
        gold_path=gold_path,
        queries=len(rows),
        low_confidence=low_confidence,
        retrieval=retrieval,
    )

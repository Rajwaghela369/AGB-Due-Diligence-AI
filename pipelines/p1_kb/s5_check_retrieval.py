"""1.5 Validate: score the gold queries against the index.

Retrieval itself lives in core.retrieval so the agent and this scorer share one
path; this module only embeds the gold file, searches it, and writes the score.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import load_yaml
from core.logging import get_logger
from core.paths import CONFIGS
from core.retrieval import embed_queries, search

log = get_logger("p1.retrieval")

# Deep enough to see where a miss actually ranked, which is what error analysis needs.
SEARCH_DEPTH = 20


@dataclass
class QueryOutcome:
    query_id: str
    topic: str
    primary_chunk_id: str
    rank: int | None  # 1-based; None when outside SEARCH_DEPTH
    retrieved: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalResult:
    report_path: Path
    metrics: dict[str, float]
    outcomes: list[QueryOutcome]

    @property
    def misses(self) -> list[QueryOutcome]:
        return [o for o in self.outcomes if o.rank is None or o.rank > 5]


def _metrics(outcomes: list[QueryOutcome]) -> dict[str, float]:
    total = len(outcomes)
    at = {
        k: sum(1 for o in outcomes if o.rank is not None and o.rank <= k) / total for k in (1, 3, 5)
    }
    mrr = sum(1 / o.rank for o in outcomes if o.rank is not None) / total
    return {
        "queries": float(total),
        "primary_at_1": round(at[1], 4),
        "primary_at_3": round(at[3], 4),
        "primary_at_5": round(at[5], 4),
        "mrr": round(mrr, 4),
    }


def check_retrieval(
    gold_path: Path,
    kb: dict[str, Any],
    report_dir: Path,
    report_name: str = "p1_retrieval_check.json",
    collection: str | None = None,
    policy: dict[str, Any] | None = None,
) -> RetrievalResult:
    """Score a gold file against the index.

    `report_name` exists so P2 can re-run this unchanged against real clause
    text and write its own report. Same code, same metrics, so the two numbers
    are directly comparable.

    `collection` is the versioned collection the P1 ingest gate just built -
    it runs before the alias moves, so scoring the alias would score the
    previous index. Left as None, the alias is read, which is what P2 wants.
    """
    gold = [json.loads(line) for line in gold_path.read_text(encoding="utf-8").splitlines()]
    policy = policy or load_yaml(CONFIGS / "app.yaml")["retrieval"]
    target = collection or kb["store"]["alias"]
    embedding = kb["embedding"]

    # One API call for the whole file, as the Phase 1 scorer always did.
    embedded = embed_queries([row["query"] for row in gold], kb)

    outcomes: list[QueryOutcome] = []
    for row, vector in zip(gold, embedded.vectors, strict=True):
        hits = search(vector, row["topic"], policy, depth=SEARCH_DEPTH, collection=target)
        retrieved = [hit.chunk_id for hit in hits]
        primary = row["primary_chunk_id"]
        outcomes.append(
            QueryOutcome(
                query_id=row["query_id"],
                topic=row["topic"],
                primary_chunk_id=primary,
                rank=retrieved.index(primary) + 1 if primary in retrieved else None,
                retrieved=retrieved[:5],
            )
        )

    metrics = _metrics(outcomes)
    metrics["query_tokens"] = float(embedded.tokens)

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / report_name
    report_path.write_text(
        json.dumps(
            {
                "collection": target,
                "embedding_model": embedding["model"],
                "query_task": embedding["task_query"],
                "policy": policy,
                "metrics": metrics,
                "queries": [
                    {
                        "query_id": o.query_id,
                        "topic": o.topic,
                        "expected": o.primary_chunk_id,
                        "rank": o.rank,
                        "top5": o.retrieved,
                    }
                    for o in outcomes
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log.info(
        "primary@1 %.2f  @3 %.2f  @5 %.2f  MRR %.3f",
        metrics["primary_at_1"],
        metrics["primary_at_3"],
        metrics["primary_at_5"],
        metrics["mrr"],
    )
    return RetrievalResult(report_path=report_path, metrics=metrics, outcomes=outcomes)


def render_misses(result: RetrievalResult) -> str:
    """Where each miss actually ranked; the input to task 1.5d."""
    if not result.misses:
        return "All gold queries put the expected chunk in the top 5."

    lines = ["| query | topic | expected | rank | top 5 |", "| --- | --- | --- | --- | --- |"]
    for outcome in result.misses:
        rank = outcome.rank or f"> {SEARCH_DEPTH}"
        lines.append(
            f"| {outcome.query_id} | {outcome.topic} | `{outcome.primary_chunk_id}` | "
            f"{rank} | {', '.join(f'`{c}`' for c in outcome.retrieved)} |"
        )
    return "\n".join(lines)

"""P2 EVAL-PREP: AGB-DE into input clauses for the agent and a separate answer key."""

import argparse
import json
from pathlib import Path

import pandas as pd
from core.config import config_hash, load_yaml
from core.paths import CONFIGS, GOLD, INTERIM, PROCESSED, RAW, REPORTS
from core.settings import load_settings
from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact

from pipelines.p2_eval.s1_fetch_agb import FetchResult, fetch_agb_snapshot
from pipelines.p2_eval.s2_normalize import NormalizeResult, normalize
from pipelines.p2_eval.s3_filter_scope import ScopeResult, filter_scope
from pipelines.p2_eval.s4_split import SplitResult, split_clauses
from pipelines.p2_eval.s5_publish import PublishResult, publish
from pipelines.p2_eval.s6_regen_gold import GoldResult, regen_gold


@task(retries=3, retry_delay_seconds=10)
def fetch_agb(source: dict) -> FetchResult:
    """2.1 Snapshot the corpus at a pinned commit, plus the authors' split."""
    result = fetch_agb_snapshot(
        repo=source["repo"],
        commit=source["commit"],
        csv_path=source["csv_path"],
        hf_dataset=source["hf_dataset"],
        out_dir=RAW / "agb-de",
        token=load_settings().hf_token or None,
    )
    get_run_logger().info(
        "snapshot %d clauses at %s, %d assigned to a split",
        result.rows,
        result.commit[:12],
        result.assigned,
    )
    return result


@task
def normalize_clauses(fetched: FetchResult, scope: dict) -> NormalizeResult:
    """2.2 One typed row per clause. Row-level only."""
    result = normalize(fetched.corpus_path, scope["fragment_chars"], INTERIM)
    get_run_logger().info(
        "normalized %d clauses, %d fragments, %d citing a §",
        result.rows,
        result.fragments,
        result.with_citations,
    )
    return result


@task
def scope_clauses(normalized: NormalizeResult, scope: dict) -> ScopeResult:
    """2.3 Narrow to the remit. Flags travel; nothing inside the slice is dropped."""
    result = filter_scope(normalized.path, scope["topics"], PROCESSED)
    get_run_logger().info(
        "scoped %d clauses (%d void), %d flagged under a foreign choice of law %s",
        result.clauses,
        result.void,
        result.foreign_law_clauses,
        result.foreign_law_contracts,
    )
    return result


@task
def split_halves(scoped: ScopeResult, fetched: FetchResult, splits: dict) -> SplitResult:
    """2.4 Attach the authors' assignment. Nothing is trained; the split is reused."""
    result = split_clauses(
        scope_path=scoped.path,
        splits_path=fetched.splits_path,
        dev=splits["dev"],
        held_out=splits["held_out"],
        out_dir=PROCESSED / "splits",
    )
    get_run_logger().info("dev %s, held out %s", result.counts, result.void)
    return result


@task
def publish_clauses_and_key(
    split: SplitResult, splits: dict, publish_cfg: dict, dry_run: bool
) -> PublishResult:
    """2.5 Two sinks, one invariant: the table holds no labels, the dataset does."""
    halves = {name: pd.read_parquet(path) for name, path in split.paths.items()}
    result = publish(
        halves=halves,
        held_out=splits["held_out"],
        table=publish_cfg["table"],
        dataset=publish_cfg["dataset"],
        dry_run=dry_run,
    )
    get_run_logger().info(
        "%s%d rows -> %s, %d items -> %s",
        "dry run: " if result.dry_run else "",
        result.table_rows,
        result.table,
        result.dataset_items,
        result.dataset,
    )
    return result


@task
def regenerate_gold(split: SplitResult, splits: dict, kb: dict) -> GoldResult:
    """2.6 Rebuild the retrieval answer key from real clause text and re-score it.

    Deliberately NOT a gate. A drop against Phase 1's synthetic 0.933 is the
    information this step exists to produce - it measures how much of that
    score came from the test being easy. Failing the run on it would punish
    the honest number.
    """
    mapping = load_yaml(CONFIGS / "gold_map.yaml")["clauses"]
    result = regen_gold(
        mapping=mapping,
        dev_path=split.paths[splits["dev"]],
        chunks_path=PROCESSED / "bgb_chunks.jsonl",
        kb=kb,
        gold_dir=GOLD,
        report_dir=REPORTS,
    )
    get_run_logger().info(
        "real-text retrieval: primary@5 %.3f, MRR %.3f, %d misses (%d low-confidence mappings)",
        result.retrieval.metrics["primary_at_5"],
        result.retrieval.metrics["mrr"],
        len(result.retrieval.misses),
        len(result.low_confidence),
    )
    return result


def check_gate(scoped: ScopeResult, split: SplitResult, splits: dict, gate: dict) -> list[dict]:
    """Every count the design doc fixed, compared against what the run produced."""
    dev, held_out = splits["dev"], splits["held_out"]
    checks = (
        ("scoped_clauses", scoped.clauses, gate["scoped_clauses"]),
        ("scoped_void", scoped.void, gate["scoped_void"]),
        ("dev_clauses", split.counts[dev], gate["dev_clauses"]),
        ("dev_void", split.void[dev], gate["dev_void"]),
        ("held_out_clauses", split.counts[held_out], gate["held_out_clauses"]),
        ("held_out_void", split.void[held_out], gate["held_out_void"]),
    )
    return [
        {"check": name, "got": got, "want": want, "passed": got == want}
        for name, got, want in checks
    ]


def write_counts(
    path: Path,
    lineage: str,
    fetched: FetchResult,
    normalized: NormalizeResult,
    scoped: ScopeResult,
    split: SplitResult,
    published: PublishResult,
    results: list[dict],
    recheck: GoldResult | None = None,
) -> None:
    """reports/p2_counts.json - the Phase 2 gate artifact, committed for its history."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "config_hash": lineage,
                "source_commit": fetched.commit,
                "corpus_rows": fetched.rows,
                "unassigned_corpus_rows": fetched.unassigned,
                "normalized_rows": normalized.rows,
                "scoped": {
                    # Asserted before any flag-based exclusion, which is why it
                    # is 523 and not 522: the one fragment is flagged, not cut.
                    "clauses": scoped.clauses,
                    "void": scoped.void,
                    "contracts": scoped.contracts,
                    "fragments": scoped.fragments,
                    "foreign_law_clauses": scoped.foreign_law_clauses,
                    "foreign_law_contracts": scoped.foreign_law_contracts,
                },
                "splits": {
                    name: {"clauses": split.counts[name], "void": split.void[name]}
                    for name in split.counts
                },
                "published": {
                    "table": published.table,
                    "table_rows": published.table_rows,
                    "dataset": published.dataset,
                    "dataset_items": published.dataset_items,
                    "dry_run": published.dry_run,
                },
                # Recorded, never gated: the comparison against Phase 1's
                # synthetic 0.933 is the point of the number.
                "retrieval_recheck": (
                    None
                    if recheck is None
                    else {
                        "queries": recheck.queries,
                        "metrics": recheck.retrieval.metrics,
                        "misses": [o.query_id for o in recheck.retrieval.misses],
                        "low_confidence_mappings": recheck.low_confidence,
                    }
                ),
                "gate": results,
                "gate_passed": all(check["passed"] for check in results),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


@flow(name="p2-retrieval-recheck")
def retrieval_recheck() -> None:
    """Step 2.6 alone, against the splits already on disk.

    Separate entry point because this is the one P2 step worth re-running by
    itself: the answer key in configs/gold_map.yaml gets revised, the clause
    data does not.
    """
    log = get_run_logger()
    splits = load_yaml(CONFIGS / "eval.yaml")["splits"]
    kb = load_yaml(CONFIGS / "kb.yaml")

    dev = splits["dev"]
    split = SplitResult(
        paths={dev: PROCESSED / "splits" / f"{dev}.parquet"},
        counts={},
        void={},
    )
    if not split.paths[dev].exists():
        raise FileNotFoundError(f"{split.paths[dev]} is missing; run `make eval-data` first")

    result = regenerate_gold(split, splits, kb)
    log.info(
        "primary@5 %.3f over %d real clauses (Phase 1 synthetic: 0.933)",
        result.retrieval.metrics["primary_at_5"],
        result.queries,
    )


@flow(name="p2-eval-prep")
def eval_prep(dry_run: bool = False, skip_recheck: bool = False) -> None:
    log = get_run_logger()
    config = load_yaml(CONFIGS / "eval.yaml")
    # P1's config decides which collection and embedding the re-check reads.
    kb = load_yaml(CONFIGS / "kb.yaml")
    lineage = config_hash(config)
    source, scope = config["source"], config["scope"]
    splits, gate, publish_cfg = config["splits"], config["gate"], config["publish"]

    log.info(
        "config %s, commit %s, dry_run=%s, skip_recheck=%s",
        lineage,
        source["commit"][:12],
        dry_run,
        skip_recheck,
    )

    fetched = fetch_agb(source)
    if fetched.rows != source["expected_rows"]:
        raise ValueError(
            f"expected {source['expected_rows']} corpus rows, snapshot has {fetched.rows}"
        )

    normalized = normalize_clauses(fetched, scope)
    scoped = scope_clauses(normalized, scope)
    split = split_halves(scoped, fetched, splits)

    # Counts are gated before anything is published: a mismatch means the
    # answer key is not the one the design doc fixed, and nothing should be
    # written off the back of it.
    results = check_gate(scoped, split, splits, gate)
    passed = all(check["passed"] for check in results)

    create_table_artifact(
        key="p2-eval-prep-gate",
        table=results,
        description=f"P2 count gate · config {lineage} · commit {source['commit'][:12]}",
    )

    if not passed:
        failures = ", ".join(
            f"{c['check']} got {c['got']} want {c['want']}" for c in results if not c["passed"]
        )
        # Written before raising, so the failed counts are on disk for analysis.
        write_counts(
            REPORTS / "p2_counts.json",
            lineage,
            fetched,
            normalized,
            scoped,
            split,
            PublishResult(publish_cfg["table"], 0, publish_cfg["dataset"], 0, True),
            results,
        )
        raise ValueError(f"P2 count gate failed: {failures}")

    published = publish_clauses_and_key(split, splits, publish_cfg, dry_run)
    recheck = None if skip_recheck else regenerate_gold(split, splits, kb)
    write_counts(
        REPORTS / "p2_counts.json",
        lineage,
        fetched,
        normalized,
        scoped,
        split,
        published,
        results,
        recheck,
    )

    create_markdown_artifact(
        key="p2-eval-prep-summary",
        markdown="\n".join(
            [
                "# P2 EVAL-PREP",
                "",
                f"- corpus: {fetched.rows} clauses at `{source['commit'][:12]}`",
                f"- scoped: {scoped.clauses} clauses, {scoped.void} void, "
                f"{scoped.contracts} contracts",
                f"- flagged: {scoped.fragments} fragment, "
                f"{scoped.foreign_law_clauses} under a foreign choice of law "
                f"(contracts {scoped.foreign_law_contracts}) - kept, not dropped",
                f"- splits: {split.counts}",
                f"- published: {published.table_rows} rows -> `{published.table}`, "
                f"{published.dataset_items} items -> `{published.dataset}`"
                f"{' (dry run)' if published.dry_run else ''}",
                *(
                    []
                    if recheck is None
                    else [
                        f"- real-text retrieval: primary@5 "
                        f"{recheck.retrieval.metrics['primary_at_5']:.3f} over "
                        f"{recheck.queries} void dev clauses "
                        f"(Phase 1 synthetic: 0.933) - recorded, not gated",
                    ]
                ),
            ]
        ),
        description="Phase 2 evaluation data preparation",
    )

    log.info("gate passed, counts written to reports/p2_counts.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="build and assert everything, write nothing to Postgres or Langfuse",
    )
    ap.add_argument(
        "--skip-recheck",
        action="store_true",
        help="skip 2.6, which calls the embedding API and reads Qdrant",
    )
    ap.add_argument(
        "--recheck-only",
        action="store_true",
        help="run only 2.6, against the splits already on disk",
    )
    args = ap.parse_args()

    if args.recheck_only:
        retrieval_recheck()
    else:
        eval_prep(dry_run=args.dry_run, skip_recheck=args.skip_recheck)

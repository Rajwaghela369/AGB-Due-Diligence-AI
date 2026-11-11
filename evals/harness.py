"""The evaluation harness. Runs any clause classifier as a Langfuse experiment.

Agent-agnostic on purpose: it takes a task function and knows nothing about how
the verdict was reached. Phase 3's one-shot baseline and Phase 4's LangGraph
agent are both just task functions, which is what makes their numbers
comparable.

Per-item evaluators record whether each clause was called correctly; the
run-level evaluator computes void-class precision, recall and F1 across the
whole run. Both land in Langfuse; the metrics are also written to
`reports/` so the gate has a file to read and git has a history.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import langfuse_client
from core.logging import get_logger
from langfuse import Evaluation

from evals.datasets import EvalItem
from evals.scorers import UNCERTAIN_VERDICT, VOID_VERDICT, score

log = get_logger("evals.harness")


@dataclass(frozen=True)
class RunResult:
    run_name: str
    split: str
    items: int
    metrics: dict[str, float]
    report_path: Path


def _verdict_of(output: Any) -> str | None:
    if isinstance(output, dict):
        return output.get("verdict")
    return None


def _field(item: Any, name: str) -> Any:
    """Read a field off an experiment item.

    Langfuse hands back a DatasetItem object for a published dataset and a
    plain dict for local data, so neither attribute nor key access works on
    its own.
    """
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def expected_void(item: Any) -> bool:
    """The label, which sits on the item rather than on the result.

    ExperimentItemResult carries `item`, `output` and `evaluations` - there is
    no `expected_output` attribute on it.
    """
    expected = _field(item, "expected_output") or {}
    return bool(expected.get("label_void"))


def clause_id_of(item: Any) -> str:
    """The clause this result belongs to.

    Without it the report's false-negative and false-positive lists are
    unusable, which is most of what the report is for.
    """
    metadata = _field(item, "metadata") or {}
    return metadata.get("clause_id") or _field(item, "id") or "?"


def item_evaluators() -> list[Any]:
    """Per-clause evaluations, so a run is inspectable in Langfuse clause by clause."""

    def correctness(*, input: Any, output: Any, expected_output: Any, **_: Any):
        verdict = _verdict_of(output)
        label = bool((expected_output or {}).get("label_void"))
        called_void = verdict == VOID_VERDICT

        return [
            Evaluation(
                name="void_correct",
                value=1.0 if called_void == label else 0.0,
                comment=f"predicted {verdict!r}, label_void={label}",
            ),
            # Separated out because an abstention is a different failure from a
            # wrong answer, and Phase 6 will route these to a human.
            Evaluation(name="abstained", value=1.0 if verdict == UNCERTAIN_VERDICT else 0.0),
            Evaluation(name="task_failed", value=1.0 if verdict is None else 0.0),
        ]

    return [correctness]


def run_evaluators(uncertain_counts_as_void: bool) -> list[Any]:
    """The aggregate void-class metrics - the numbers a phase gate reads."""

    def void_class(*, item_results: list[Any], **_: Any):
        predictions = [_verdict_of(r.output) for r in item_results]
        labels = [expected_void(r.item) for r in item_results]
        metrics = score(predictions, labels, uncertain_counts_as_void)

        return [
            Evaluation(name=name, value=value)
            for name, value in metrics.items()
            # The counts are useful in the report but would clutter Langfuse's
            # run comparison, which is driven by the three headline metrics.
            if name in ("void_precision", "void_recall", "void_f1")
        ] + [
            Evaluation(
                name="void_counts",
                value=(
                    f"tp={int(metrics['true_positive'])} fp={int(metrics['false_positive'])} "
                    f"fn={int(metrics['false_negative'])} tn={int(metrics['true_negative'])} "
                    f"uncertain={int(metrics['uncertain'])} errors={int(metrics['errors'])}"
                ),
                data_type="CATEGORICAL",
            )
        ]

    return [void_class]


def write_report(
    path: Path,
    run_name: str,
    split: str,
    metrics: dict[str, float],
    config: dict[str, Any],
    per_item: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_name": run_name,
                "split": split,
                "config": config,
                "metrics": metrics,
                # Named so error analysis can start from the file rather than
                # the Langfuse UI.
                "false_negatives": [r["clause_id"] for r in per_item if r["false_negative"]],
                "false_positives": [r["clause_id"] for r in per_item if r["false_positive"]],
                "failures": [r["clause_id"] for r in per_item if r["verdict"] is None],
                "items": per_item,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def run(
    name: str,
    run_name: str,
    items: list[EvalItem],
    task: Any,
    split: str,
    config: dict[str, Any],
    report_path: Path,
    dataset_name: str | None = None,
    max_concurrency: int = 8,
    uncertain_counts_as_void: bool = False,
) -> RunResult:
    """Execute one experiment and record it in Langfuse and in a report file."""
    client = langfuse_client.client()

    # Held-out runs attach to the published dataset so Langfuse can compare
    # them across phases; dev runs pass local data and are compared by name.
    if dataset_name:
        data = list(client.get_dataset(dataset_name).items)
    else:
        data = [item.as_local() for item in items]

    log.info("running %s over %d %s items", run_name, len(data), split)
    result = client.run_experiment(
        name=name,
        run_name=run_name,
        description=f"{split} split, {len(data)} clauses",
        data=data,
        task=task,
        evaluators=item_evaluators(),
        run_evaluators=run_evaluators(uncertain_counts_as_void),
        max_concurrency=max_concurrency,
        metadata={key: str(value) for key, value in config.items()},
    )

    by_id = {item.id: item for item in items}
    per_item, predictions, labels = [], [], []
    for entry in result.item_results:
        clause_id = clause_id_of(entry.item)
        verdict = _verdict_of(entry.output)
        label = expected_void(entry.item)
        predictions.append(verdict)
        labels.append(label)
        per_item.append(
            {
                "clause_id": clause_id,
                "label_void": label,
                "verdict": verdict,
                "false_negative": label and verdict != VOID_VERDICT,
                "false_positive": (not label) and verdict == VOID_VERDICT,
                "reason_de": (entry.output or {}).get("reason_de", "") if entry.output else "",
                "error": (entry.output or {}).get("error") if entry.output else None,
                "in_dataset": clause_id in by_id,
            }
        )

    metrics = score(predictions, labels, uncertain_counts_as_void)
    write_report(report_path, run_name, split, metrics, config, per_item)
    client.flush()

    log.info(
        "%s: void P %.3f R %.3f F1 %.3f (tp=%d fp=%d fn=%d, uncertain=%d, errors=%d)",
        run_name,
        metrics["void_precision"],
        metrics["void_recall"],
        metrics["void_f1"],
        int(metrics["true_positive"]),
        int(metrics["false_positive"]),
        int(metrics["false_negative"]),
        int(metrics["uncertain"]),
        int(metrics["errors"]),
    )
    return RunResult(
        run_name=run_name,
        split=split,
        items=len(per_item),
        metrics=metrics,
        report_path=report_path,
    )

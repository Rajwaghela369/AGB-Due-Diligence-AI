"""The evaluation harness. Runs any clause classifier as a Langfuse experiment.

Agent-agnostic on purpose: it takes a task function and knows nothing about how
the verdict was reached. Phase 3's one-shot baseline and Phase 4's LangGraph
agent are both just task functions, which is what makes their numbers
comparable.

Per-item evaluators record whether each clause was called correctly; the
run-level evaluators compute void-class precision, recall and F1 and the
grounding rate across the whole run. Both land in Langfuse; the metrics are
also written to `reports/` so the gate has a file to read and git has a history.

Task output contract, which both the baseline and the agent honour:

    {"verdict": str | None, "legal_basis": [chunk_id], "candidates": [chunk_id],
     "reason_de": str, "confidence": float, "error": str | None}

The baseline emits no `legal_basis` or `candidates`; missing keys read as [].
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core import langfuse_client
from core.budget import BudgetExceeded, RequestBudget
from core.logging import get_logger
from langfuse import Evaluation

from evals.datasets import EvalItem
from evals.scorers import UNCERTAIN_VERDICT, VOID_VERDICT, score, weighted_metrics

log = get_logger("evals.harness")


@dataclass(frozen=True)
class RunResult:
    run_name: str
    split: str
    items: int
    metrics: dict[str, float]
    report_path: Path
    requests_spent: int = 0


def _verdict_of(output: Any) -> str | None:
    if isinstance(output, dict):
        return output.get("verdict")
    return None


def _ids_of(output: Any, key: str) -> list[str]:
    """`legal_basis` or `candidates` off a task output; [] when absent or malformed."""
    if not isinstance(output, dict):
        return []
    value = output.get(key) or []
    return [str(v) for v in value] if isinstance(value, list) else []


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


def sample_weight_of(item: Any) -> float:
    """How many population clauses this one stands for.

    1.0 unless a stratified slice marked it otherwise. Defaulting to 1.0 rather
    than 0.0 means an unsampled run's weighted metrics equal its raw ones.
    """
    metadata = _field(item, "metadata") or {}
    try:
        return float(metadata.get("sample_weight", 1.0))
    except (TypeError, ValueError):
        return 1.0


def stratum_of(item: Any) -> str:
    metadata = _field(item, "metadata") or {}
    return str(metadata.get("stratum") or ("void" if expected_void(item) else "non_void"))


@dataclass(frozen=True)
class Grounding:
    """Whether a void verdict rests on evidence the judge was actually shown.

    Only a `potentially_void` verdict can be grounded or not; `applies` is
    False for every other verdict, and nothing about them is scored. Counting
    a "valid" or a failed call as grounded would let a run where every call
    failed score 100% - the same accuracy-shaped trap `void_correct` fell into.
    """

    applies: bool
    grounded: bool
    outside_candidates: bool  # cited an id the judge was never shown

    @classmethod
    def of(cls, output: Any) -> "Grounding":
        if _verdict_of(output) != VOID_VERDICT:
            return cls(applies=False, grounded=False, outside_candidates=False)
        basis, candidates = _ids_of(output, "legal_basis"), set(_ids_of(output, "candidates"))
        outside = any(chunk_id not in candidates for chunk_id in basis)
        return cls(applies=True, grounded=bool(basis) and not outside, outside_candidates=outside)


def grounding_metrics(outputs: list[Any]) -> dict[str, float]:
    """Grounding over void verdicts only.

    With zero void verdicts the rate is 1.0: nothing was asserted without
    evidence. That makes a never-void run look grounded, which is why the gate
    reads void F1 beside it rather than this number alone.
    """
    states = [Grounding.of(output) for output in outputs]
    void = [s for s in states if s.applies]
    grounded = sum(1 for s in void if s.grounded)
    outside = sum(1 for s in void if s.outside_candidates)
    return {
        "grounding_rate": 1.0 if not void else round(grounded / len(void), 4),
        "void_calls": float(len(void)),
        "grounded": float(grounded),
        "ungrounded_outside_candidates": float(outside),
    }


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
            # wrong answer, and Phase 7 will route these to a human.
            Evaluation(name="abstained", value=1.0 if verdict == UNCERTAIN_VERDICT else 0.0),
            Evaluation(name="task_failed", value=1.0 if verdict is None else 0.0),
        ]

    def grounded(*, input: Any, output: Any, expected_output: Any, **_: Any):
        # Emitted only for void verdicts. Langfuse averages per-item scores, so
        # emitting a placeholder for the rest would put the average back on the
        # wrong denominator; omitting keeps it over void calls, like the run
        # metric.
        state = Grounding.of(output)
        if not state.applies:
            return []
        return [
            Evaluation(
                name="grounded",
                value=1.0 if state.grounded else 0.0,
                comment=(
                    "cited a chunk outside the candidates"
                    if state.outside_candidates
                    else ("no legal basis" if not state.grounded else "basis within candidates")
                ),
            )
        ]

    return [correctness, grounded]


def run_evaluators(uncertain_counts_as_void: bool) -> list[Any]:
    """The aggregate metrics - the numbers a phase gate reads."""

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

    def grounding(*, item_results: list[Any], **_: Any):
        metrics = grounding_metrics([r.output for r in item_results])
        return [
            Evaluation(name="grounding_rate", value=metrics["grounding_rate"]),
            # `ungrounded_ids` is the hallucinated-citation case. The judge's
            # schema enum should make it impossible; this is the independent proof.
            Evaluation(
                name="grounding_counts",
                value=(
                    f"grounded={int(metrics['grounded'])} void={int(metrics['void_calls'])} "
                    f"ungrounded_ids={int(metrics['ungrounded_outside_candidates'])}"
                ),
                data_type="CATEGORICAL",
            ),
        ]

    return [void_class, grounding]


def write_report(
    path: Path,
    run_name: str,
    split: str,
    metrics: dict[str, float],
    config: dict[str, Any],
    per_item: list[dict[str, Any]],
    requests_spent: int = 0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_name": run_name,
                "split": split,
                "config": config,
                "metrics": metrics,
                "requests_spent": requests_spent,
                # Named so error analysis can start from the file rather than
                # the Langfuse UI.
                "false_negatives": [r["clause_id"] for r in per_item if r["false_negative"]],
                "false_positives": [r["clause_id"] for r in per_item if r["false_positive"]],
                "failures": [r["clause_id"] for r in per_item if r["verdict"] is None],
                # Void verdicts with no basis or a basis outside the candidates.
                "ungrounded": [r["clause_id"] for r in per_item if r.get("grounded") is False],
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
    budget: RequestBudget | None = None,
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
    try:
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
        item_results = list(result.item_results)
    except BudgetExceeded as exc:
        # A quota stop must still leave an analysable run. Langfuse normally
        # catches task exceptions per item, so reaching here means the budget
        # tripped outside a task; report what exists rather than losing it.
        log.warning("request budget exhausted, reporting the partial run: %s", exc)
        item_results = []

    by_id = {item.id: item for item in items}
    per_item, predictions, labels, outputs, weights = [], [], [], [], []
    for entry in item_results:
        clause_id = clause_id_of(entry.item)
        output = entry.output if isinstance(entry.output, dict) else {}
        verdict = _verdict_of(output)
        label = expected_void(entry.item)
        state = Grounding.of(output)
        weight = sample_weight_of(entry.item)
        predictions.append(verdict)
        labels.append(label)
        outputs.append(output)
        weights.append(weight)
        per_item.append(
            {
                "clause_id": clause_id,
                "label_void": label,
                "verdict": verdict,
                "false_negative": label and verdict != VOID_VERDICT,
                "false_positive": (not label) and verdict == VOID_VERDICT,
                "legal_basis": _ids_of(output, "legal_basis"),
                "candidates": _ids_of(output, "candidates"),
                # None for non-void verdicts: grounding does not apply to them.
                "grounded": state.grounded if state.applies else None,
                "stratum": stratum_of(entry.item),
                "sample_weight": weight,
                "void_probability": output.get("void_probability"),
                "input_tokens": int(output.get("input_tokens") or 0),
                "output_tokens": int(output.get("output_tokens") or 0),
                "reason_de": output.get("reason_de", ""),
                "error": output.get("error"),
                "in_dataset": clause_id in by_id,
            }
        )

    spent = budget.spent() if budget else 0
    metrics = {
        **weighted_metrics(predictions, labels, weights, uncertain_counts_as_void),
        **grounding_metrics(outputs),
    }
    write_report(report_path, run_name, split, metrics, config, per_item, spent)
    client.flush()

    log.info(
        "%s: void P %.3f (weighted %.3f) R %.3f F1 %.3f (weighted %.3f) "
        "tp=%d fp=%d fn=%d uncertain=%d errors=%d | grounding %.3f over %d void calls | "
        "%d requests",
        run_name,
        metrics["void_precision"],
        metrics["void_precision_weighted"],
        metrics["void_recall"],
        metrics["void_f1"],
        metrics["void_f1_weighted"],
        int(metrics["true_positive"]),
        int(metrics["false_positive"]),
        int(metrics["false_negative"]),
        int(metrics["uncertain"]),
        int(metrics["errors"]),
        metrics["grounding_rate"],
        int(metrics["void_calls"]),
        spent,
    )
    return RunResult(
        run_name=run_name,
        split=split,
        items=len(per_item),
        metrics=metrics,
        report_path=report_path,
        requests_spent=spent,
    )

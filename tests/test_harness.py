import json
from dataclasses import dataclass
from typing import Any

from evals.harness import item_evaluators, run_evaluators, write_report


@dataclass
class _ItemResult:
    """Mirrors ExperimentItemResult: the label sits on `item`, not on the result."""

    output: Any
    item: dict


def _evaluate_item(output, label):
    (correctness,) = item_evaluators()
    return {
        e.name: e.value
        for e in correctness(input={}, output=output, expected_output={"label_void": label})
    }


def test_item_evaluator_marks_a_correct_void_call():
    scores = _evaluate_item({"verdict": "potentially_void"}, True)

    assert scores == {"void_correct": 1.0, "abstained": 0.0, "task_failed": 0.0}


def test_item_evaluator_marks_a_missed_void_clause():
    scores = _evaluate_item({"verdict": "valid"}, True)

    assert scores["void_correct"] == 0.0


def test_item_evaluator_separates_abstention_from_a_wrong_answer():
    # Phase 6 routes abstentions to a human, so they must be countable apart
    # from ordinary errors.
    scores = _evaluate_item({"verdict": "uncertain"}, True)

    assert scores["abstained"] == 1.0
    assert scores["task_failed"] == 0.0
    assert scores["void_correct"] == 0.0


def test_item_evaluator_flags_a_task_failure():
    scores = _evaluate_item({"verdict": None, "error": "RuntimeError: boom"}, True)

    assert scores["task_failed"] == 1.0


def test_item_evaluator_survives_a_non_dict_output():
    scores = _evaluate_item("the model returned prose", False)

    assert scores["task_failed"] == 1.0
    # A null verdict on a valid clause is still the right call, by luck.
    assert scores["void_correct"] == 1.0


def test_run_evaluator_reports_the_three_headline_metrics_and_the_counts():
    (void_class,) = run_evaluators(uncertain_counts_as_void=False)
    results = [
        _ItemResult({"verdict": "potentially_void"}, {"expected_output": {"label_void": True}}),
        _ItemResult({"verdict": "potentially_void"}, {"expected_output": {"label_void": False}}),
        _ItemResult({"verdict": "valid"}, {"expected_output": {"label_void": True}}),
        _ItemResult({"verdict": "valid"}, {"expected_output": {"label_void": False}}),
    ]

    scores = {e.name: e.value for e in void_class(item_results=results)}

    assert scores["void_precision"] == 0.5
    assert scores["void_recall"] == 0.5
    assert scores["void_f1"] == 0.5
    # Counts travel as one categorical string to keep Langfuse's run
    # comparison readable.
    assert scores["void_counts"] == "tp=1 fp=1 fn=1 tn=1 uncertain=0 errors=0"


def test_run_evaluator_honours_the_uncertain_setting():
    results = [_ItemResult({"verdict": "uncertain"}, {"expected_output": {"label_void": True}})]

    strict = {e.name: e.value for e in run_evaluators(False)[0](item_results=results)}
    lenient = {e.name: e.value for e in run_evaluators(True)[0](item_results=results)}

    assert strict["void_recall"] == 0.0
    assert lenient["void_recall"] == 1.0


def test_report_names_the_clauses_worth_looking_at(tmp_path):
    path = tmp_path / "p3_baseline_dev.json"
    per_item = [
        {
            "clause_id": "agb-349",
            "verdict": "valid",
            "false_negative": True,
            "false_positive": False,
        },
        {
            "clause_id": "agb-350",
            "verdict": "potentially_void",
            "false_negative": False,
            "false_positive": True,
        },
        {"clause_id": "agb-351", "verdict": None, "false_negative": True, "false_positive": False},
    ]

    write_report(
        path, "baseline-dev-x", "dev", {"void_f1": 0.5}, {"model": "gpt-4o-mini"}, per_item
    )
    report = json.loads(path.read_text(encoding="utf-8"))

    # Error analysis should start from the file, not the Langfuse UI.
    assert report["false_negatives"] == ["agb-349", "agb-351"]
    assert report["false_positives"] == ["agb-350"]
    assert report["failures"] == ["agb-351"]
    assert report["run_name"] == "baseline-dev-x"
    assert report["config"]["model"] == "gpt-4o-mini"


def test_clause_id_reads_both_item_shapes():
    # Langfuse gives a DatasetItem object for a published dataset and a plain
    # dict for local data. Getting this wrong empties the report's
    # false-negative list without failing anything.
    from evals.datasets import EvalItem
    from evals.harness import clause_id_of

    local = EvalItem("agb-349", {"text": "x"}, {"label_void": True}, {"split": "dev"}).as_local()
    published = type("DatasetItem", (), {"id": "agb-606", "metadata": {"split": "test"}})()
    no_metadata = type("DatasetItem", (), {"id": "agb-700", "metadata": None})()

    assert clause_id_of(local) == "agb-349"
    assert clause_id_of(published) == "agb-606"
    assert clause_id_of(no_metadata) == "agb-700"
    assert clause_id_of({}) == "?"

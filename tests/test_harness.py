import json
from dataclasses import dataclass
from typing import Any

from evals.harness import (
    Grounding,
    grounding_metrics,
    item_evaluators,
    run_evaluators,
    sample_weight_of,
    stratum_of,
    write_report,
)


@dataclass
class _ItemResult:
    """Mirrors ExperimentItemResult: the label sits on `item`, not on the result."""

    output: Any
    item: dict


def _item_scores(output, label):
    """Every per-item score, from every evaluator, keyed by the name it emits.

    Selecting by emitted name rather than list position is what lets
    evaluators be added without rewriting these tests.
    """
    scores = {}
    for evaluator in item_evaluators():
        for e in evaluator(input={}, output=output, expected_output={"label_void": label}):
            scores[e.name] = e.value
    return scores


def _run_scores(results, uncertain_counts_as_void=False):
    scores = {}
    for evaluator in run_evaluators(uncertain_counts_as_void):
        for e in evaluator(item_results=results):
            scores[e.name] = e.value
    return scores


def _result(verdict, label, basis=None, candidates=None):
    output = {"verdict": verdict}
    if basis is not None:
        output["legal_basis"] = basis
    if candidates is not None:
        output["candidates"] = candidates
    return _ItemResult(output, {"expected_output": {"label_void": label}})


# --- correctness ------------------------------------------------------------


def test_item_evaluator_marks_a_correct_void_call():
    scores = _item_scores({"verdict": "potentially_void"}, True)

    assert scores["void_correct"] == 1.0
    assert scores["abstained"] == 0.0
    assert scores["task_failed"] == 0.0


def test_item_evaluator_marks_a_missed_void_clause():
    scores = _item_scores({"verdict": "valid"}, True)

    assert scores["void_correct"] == 0.0


def test_item_evaluator_separates_abstention_from_a_wrong_answer():
    # Phase 7 routes abstentions to a human, so they must be countable apart
    # from ordinary errors.
    scores = _item_scores({"verdict": "uncertain"}, True)

    assert scores["abstained"] == 1.0
    assert scores["task_failed"] == 0.0
    assert scores["void_correct"] == 0.0


def test_item_evaluator_flags_a_task_failure():
    scores = _item_scores({"verdict": None, "error": "RuntimeError: boom"}, True)

    assert scores["task_failed"] == 1.0


def test_item_evaluator_survives_a_non_dict_output():
    scores = _item_scores("the model returned prose", False)

    assert scores["task_failed"] == 1.0
    # A null verdict on a valid clause is still the right call, by luck.
    assert scores["void_correct"] == 1.0


# --- grounding, per item ----------------------------------------------------


def test_void_with_basis_inside_candidates_is_grounded():
    scores = _item_scores(
        {
            "verdict": "potentially_void",
            "legal_basis": ["bgb-309-7-a"],
            "candidates": ["bgb-309-7-a", "bgb-276-abs1"],
        },
        True,
    )

    assert scores["grounded"] == 1.0


def test_void_with_empty_basis_is_not_grounded():
    scores = _item_scores(
        {"verdict": "potentially_void", "legal_basis": [], "candidates": ["bgb-309-7-a"]}, True
    )

    assert scores["grounded"] == 0.0


def test_void_citing_a_chunk_outside_the_candidates_is_not_grounded():
    # The hallucinated-citation case the judge's schema enum should make
    # impossible. The evaluator is the independent proof that it is.
    output = {
        "verdict": "potentially_void",
        "legal_basis": ["bgb-999"],
        "candidates": ["bgb-309-7-a"],
    }

    assert _item_scores(output, True)["grounded"] == 0.0
    assert Grounding.of(output).outside_candidates is True


def test_non_void_verdicts_emit_no_grounded_score():
    # Counting a "valid" or a failed call as grounded is how a run where every
    # call failed would score 100%.
    for output in ({"verdict": "valid"}, {"verdict": "uncertain"}, {"verdict": None}, "prose"):
        assert "grounded" not in _item_scores(output, False)
        assert Grounding.of(output).applies is False


def test_baseline_output_without_basis_keys_still_evaluates():
    # Phase 3's task emits neither legal_basis nor candidates.
    state = Grounding.of({"verdict": "potentially_void"})

    assert state.applies is True
    assert state.grounded is False
    assert state.outside_candidates is False


# --- run level ----------------------------------------------------------------


def test_run_evaluator_reports_the_three_headline_metrics_and_the_counts():
    results = [
        _result("potentially_void", True),
        _result("potentially_void", False),
        _result("valid", True),
        _result("valid", False),
    ]

    scores = _run_scores(results)

    assert scores["void_precision"] == 0.5
    assert scores["void_recall"] == 0.5
    assert scores["void_f1"] == 0.5
    # Counts travel as one categorical string to keep Langfuse's run
    # comparison readable.
    assert scores["void_counts"] == "tp=1 fp=1 fn=1 tn=1 uncertain=0 errors=0"


def test_run_evaluator_honours_the_uncertain_setting():
    results = [_result("uncertain", True)]

    assert _run_scores(results, uncertain_counts_as_void=False)["void_recall"] == 0.0
    assert _run_scores(results, uncertain_counts_as_void=True)["void_recall"] == 1.0


def test_grounding_rate_is_over_void_verdicts_only():
    results = [
        _result("potentially_void", True, basis=["a"], candidates=["a"]),
        _result("potentially_void", False, basis=[], candidates=["a"]),
        _result("potentially_void", True, basis=["zzz"], candidates=["a"]),
        _result("valid", False),
        _result("uncertain", True),
    ]

    scores = _run_scores(results)

    # 1 grounded of 3 void verdicts; the valid and uncertain ones do not count.
    assert scores["grounding_rate"] == 0.3333
    assert scores["grounding_counts"] == "grounded=1 void=3 ungrounded_ids=1"


def test_grounding_rate_is_one_when_nothing_was_called_void():
    # Nothing was asserted without evidence. Read beside void F1, never alone.
    results = [_result("valid", False), _result("uncertain", True)]

    scores = _run_scores(results)

    assert scores["grounding_rate"] == 1.0
    assert scores["grounding_counts"] == "grounded=0 void=0 ungrounded_ids=0"


def test_a_run_of_failures_scores_zero_f1_not_perfect_grounding_as_quality():
    # The two metrics are decoupled on purpose: grounding says nothing about a
    # run that never produced a verdict, and F1 says it produced none.
    results = [_result(None, True), _result(None, False), _result(None, True)]

    scores = _run_scores(results)

    assert scores["grounding_rate"] == 1.0
    assert scores["void_f1"] == 0.0
    assert scores["void_counts"].endswith("errors=3")
    assert all(_item_scores(r.output, True)["task_failed"] == 1.0 for r in results)


def test_grounding_metrics_counts():
    outputs = [
        {"verdict": "potentially_void", "legal_basis": ["a"], "candidates": ["a"]},
        {"verdict": "potentially_void", "legal_basis": ["b"], "candidates": ["a"]},
        {"verdict": "valid"},
    ]

    metrics = grounding_metrics(outputs)

    assert metrics == {
        "grounding_rate": 0.5,
        "void_calls": 2.0,
        "grounded": 1.0,
        "ungrounded_outside_candidates": 1.0,
    }


# --- report -------------------------------------------------------------------


def test_report_names_the_clauses_worth_looking_at(tmp_path):
    path = tmp_path / "p4_agent_dev.json"
    per_item = [
        {
            "clause_id": "agb-349",
            "verdict": "valid",
            "false_negative": True,
            "false_positive": False,
            "grounded": None,
        },
        {
            "clause_id": "agb-350",
            "verdict": "potentially_void",
            "false_negative": False,
            "false_positive": True,
            "grounded": False,
        },
        {
            "clause_id": "agb-351",
            "verdict": None,
            "false_negative": True,
            "false_positive": False,
            "grounded": None,
        },
        {
            "clause_id": "agb-352",
            "verdict": "potentially_void",
            "false_negative": False,
            "false_positive": False,
            "grounded": True,
        },
    ]

    write_report(
        path,
        "agent-dev-x",
        "dev",
        {"void_f1": 0.5, "grounding_rate": 0.5},
        {"model": "claude-sonnet-5"},
        per_item,
    )
    report = json.loads(path.read_text(encoding="utf-8"))

    # Error analysis should start from the file, not the Langfuse UI.
    assert report["false_negatives"] == ["agb-349", "agb-351"]
    assert report["false_positives"] == ["agb-350"]
    assert report["failures"] == ["agb-351"]
    # Only void verdicts can be ungrounded; None (not applicable) is not listed.
    assert report["ungrounded"] == ["agb-350"]
    assert report["metrics"]["grounding_rate"] == 0.5
    assert report["run_name"] == "agent-dev-x"
    assert report["config"]["model"] == "claude-sonnet-5"


def test_report_tolerates_phase_3_items_without_a_grounded_key(tmp_path):
    path = tmp_path / "p3_baseline_dev.json"
    per_item = [
        {"clause_id": "agb-1", "verdict": "valid", "false_negative": False, "false_positive": False}
    ]

    write_report(path, "baseline-dev-x", "dev", {"void_f1": 0.0}, {}, per_item)

    assert json.loads(path.read_text(encoding="utf-8"))["ungrounded"] == []


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


# --- stratified weights and budget accounting --------------------------------


def test_sample_weight_defaults_to_one_when_a_slice_did_not_set_it():
    # An unsliced run's weighted metrics must equal its raw ones, so a missing
    # key reads as 1.0 rather than 0.0.
    assert sample_weight_of({"metadata": {}}) == 1.0
    assert sample_weight_of({}) == 1.0
    assert sample_weight_of({"metadata": {"sample_weight": 9.9487}}) == 9.9487


def test_sample_weight_survives_a_malformed_value():
    assert sample_weight_of({"metadata": {"sample_weight": "nonsense"}}) == 1.0
    assert sample_weight_of({"metadata": {"sample_weight": None}}) == 1.0


def test_sample_weight_reads_both_item_shapes():
    published = type("DatasetItem", (), {"metadata": {"sample_weight": 4.0}})()

    assert sample_weight_of(published) == 4.0


def test_stratum_falls_back_to_the_label_when_unset():
    assert stratum_of({"expected_output": {"label_void": True}}) == "void"
    assert stratum_of({"expected_output": {"label_void": False}}) == "non_void"
    assert stratum_of({"metadata": {"stratum": "void"}}) == "void"


def test_report_carries_the_weighted_metrics_and_the_request_count(tmp_path):
    path = tmp_path / "p4_agent_dev70.json"
    per_item = [
        {
            "clause_id": "agb-349",
            "verdict": "potentially_void",
            "false_negative": False,
            "false_positive": False,
            "grounded": True,
            "stratum": "void",
            "sample_weight": 1.0,
        }
    ]
    metrics = {"void_f1": 0.5, "void_f1_weighted": 0.2, "void_precision_weighted": 0.1}

    write_report(path, "agent-dev70-x", "dev", metrics, {"approach": "agentic RAG"}, per_item, 71)
    report = json.loads(path.read_text(encoding="utf-8"))

    # Both numbers travel together so neither can be quoted as the other.
    assert report["metrics"]["void_f1"] == 0.5
    assert report["metrics"]["void_f1_weighted"] == 0.2
    assert report["requests_spent"] == 71
    assert report["items"][0]["sample_weight"] == 1.0
    assert report["items"][0]["stratum"] == "void"

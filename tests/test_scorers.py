import pytest

from evals.scorers import confusion, score, weighted_metrics

VOID = "potentially_void"
VALID = "valid"
UNCERTAIN = "uncertain"


def test_perfect_prediction():
    metrics = score([VOID, VALID, VOID], [True, False, True])

    assert metrics["void_precision"] == 1.0
    assert metrics["void_recall"] == 1.0
    assert metrics["void_f1"] == 1.0


def test_always_valid_scores_zero_not_high_accuracy():
    # The reason void-class metrics exist: at a 7.5% base rate this model is
    # 92.5% accurate and completely useless.
    labels = [False] * 12 + [True]
    metrics = score([VALID] * 13, labels)

    assert metrics["void_recall"] == 0.0
    assert metrics["void_precision"] == 0.0
    assert metrics["void_f1"] == 0.0
    assert metrics["false_negative"] == 1.0


def test_always_void_has_perfect_recall_and_poor_precision():
    labels = [True, False, False, False]
    metrics = score([VOID] * 4, labels)

    assert metrics["void_recall"] == 1.0
    assert metrics["void_precision"] == 0.25
    assert metrics["void_f1"] == 0.4


def test_uncertain_costs_recall_by_default():
    metrics = score([UNCERTAIN, UNCERTAIN], [True, False])

    # Declining to decide is not a void call.
    assert metrics["void_recall"] == 0.0
    assert metrics["false_negative"] == 1.0
    assert metrics["true_negative"] == 1.0
    assert metrics["uncertain"] == 2.0


def test_uncertain_can_be_counted_as_void():
    metrics = score([UNCERTAIN, UNCERTAIN], [True, False], uncertain_counts_as_void=True)

    assert metrics["void_recall"] == 1.0
    assert metrics["true_positive"] == 1.0
    assert metrics["false_positive"] == 1.0


def test_a_failed_call_costs_recall_rather_than_shrinking_the_denominator():
    # None is a crash or an unparseable answer. Dropping those items would
    # flatter the score.
    metrics = score([None, None, VOID], [True, False, True])

    assert metrics["errors"] == 2.0
    assert metrics["items"] == 3.0
    assert metrics["false_negative"] == 1.0
    assert metrics["void_recall"] == 0.5


def test_mixed_case_arithmetic():
    predictions = [VOID, VOID, VALID, VALID, VOID]
    labels = [True, False, True, False, True]

    matrix = confusion(predictions, labels)

    assert (matrix.true_positive, matrix.false_positive) == (2, 1)
    assert (matrix.false_negative, matrix.true_negative) == (1, 1)
    metrics = score(predictions, labels)
    assert metrics["void_precision"] == pytest.approx(0.6667, abs=1e-4)
    assert metrics["void_recall"] == pytest.approx(0.6667, abs=1e-4)


def test_length_mismatch_is_an_error():
    with pytest.raises(ValueError, match="2 predictions for 3 labels"):
        confusion([VOID, VALID], [True, False, True])


def test_empty_run_does_not_divide_by_zero():
    metrics = score([], [])

    assert metrics["void_f1"] == 0.0
    assert metrics["items"] == 0.0


# --- weighted precision: correcting a void-dense slice back to the base rate ---


def test_weighted_equals_raw_when_every_weight_is_one():
    predictions = [VOID, VALID, VOID, VALID]
    labels = [True, False, True, False]
    weights = [1.0] * 4

    raw = score(predictions, labels)
    weighted = weighted_metrics(predictions, labels, weights)

    assert weighted["void_precision_weighted"] == raw["void_precision"]
    assert weighted["void_f1_weighted"] == raw["void_f1"]


def test_weighting_deflates_precision_toward_the_population_rate():
    # 1 TP and 1 FP: raw precision 0.5. The FP came from a stratum sampled at
    # 1-in-10, so it stands for 10 clauses: 1/(1+10) = 0.0909.
    predictions = [VOID, VOID]
    labels = [True, False]
    weights = [1.0, 10.0]

    metrics = weighted_metrics(predictions, labels, weights)

    assert metrics["void_precision"] == 0.5
    assert metrics["void_precision_weighted"] == pytest.approx(0.0909, abs=1e-4)
    assert metrics["false_positive_weighted"] == 10.0


def test_weighting_leaves_recall_untouched():
    # Every void clause is present, so recall needs no correction - which is
    # why only precision and F1 gain a weighted form.
    predictions = [VOID, VALID, VOID]
    labels = [True, True, False]
    weights = [1.0, 1.0, 9.9487]

    metrics = weighted_metrics(predictions, labels, weights)

    assert metrics["void_recall"] == 0.5
    assert metrics["void_precision"] == 0.5
    assert metrics["void_precision_weighted"] < metrics["void_precision"]


def test_weighted_metrics_passes_the_raw_numbers_through():
    metrics = weighted_metrics([VOID], [True], [1.0])

    # A report must carry both, so neither can be mistaken for the other.
    assert metrics["void_precision"] == 1.0
    assert metrics["void_precision_weighted"] == 1.0
    assert metrics["true_positive"] == 1.0


def test_weighted_metrics_rejects_a_length_mismatch():
    with pytest.raises(ValueError, match="2 predictions, 2 labels, 1 weights"):
        weighted_metrics([VOID, VALID], [True, False], [1.0])


def test_weighted_metrics_on_an_empty_run_does_not_divide_by_zero():
    metrics = weighted_metrics([], [], [])

    assert metrics["void_precision_weighted"] == 0.0
    assert metrics["void_f1_weighted"] == 0.0

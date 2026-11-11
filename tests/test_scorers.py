import pytest

from evals.scorers import confusion, score

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

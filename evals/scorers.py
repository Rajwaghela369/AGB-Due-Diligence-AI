"""Void-class scoring. The only numbers Phase 3 onwards is judged on.

Precision, recall and F1 are computed on the void class alone, never averaged
across both classes. With a 7.5% base rate, a model that answers "valid" to
everything scores 92.5% accuracy and is worthless; the same model scores 0.0
recall here, which is the truth.
"""

from dataclasses import dataclass

from core.schemas import Verdict

VOID_VERDICT: Verdict = "potentially_void"
UNCERTAIN_VERDICT: Verdict = "uncertain"


@dataclass(frozen=True)
class Confusion:
    """Counts over the void class, plus how often the model declined to decide."""

    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    uncertain: int
    errors: int

    @property
    def total(self) -> int:
        return self.true_positive + self.false_positive + self.false_negative + self.true_negative


def _ratio(numerator: int, denominator: int) -> float:
    """Zero rather than NaN when the denominator is empty.

    A model that never predicts void has undefined precision in the
    mathematical sense; reporting 0.0 is the useful reading and keeps the
    metric comparable across runs.
    """
    return 0.0 if denominator == 0 else round(numerator / denominator, 4)


def confusion(
    predictions: list[Verdict | None],
    labels: list[bool],
    uncertain_counts_as_void: bool = False,
) -> Confusion:
    """Tally predictions against the authors' labels.

    `None` is a task failure - a crashed call or an unparseable answer. It is
    counted as "not void", so a failure costs recall rather than silently
    shrinking the denominator and flattering the score.
    """
    if len(predictions) != len(labels):
        raise ValueError(f"{len(predictions)} predictions for {len(labels)} labels")

    counts = dict.fromkeys(
        ("true_positive", "false_positive", "false_negative", "true_negative"), 0
    )
    uncertain = sum(1 for p in predictions if p == UNCERTAIN_VERDICT)
    errors = sum(1 for p in predictions if p is None)

    for prediction, label in zip(predictions, labels, strict=True):
        called_void = prediction == VOID_VERDICT or (
            uncertain_counts_as_void and prediction == UNCERTAIN_VERDICT
        )
        if label and called_void:
            counts["true_positive"] += 1
        elif label:
            counts["false_negative"] += 1
        elif called_void:
            counts["false_positive"] += 1
        else:
            counts["true_negative"] += 1

    return Confusion(**counts, uncertain=uncertain, errors=errors)


def void_metrics(matrix: Confusion) -> dict[str, float]:
    """Precision, recall and F1 on the void class, plus the diagnostic counts."""
    precision = _ratio(matrix.true_positive, matrix.true_positive + matrix.false_positive)
    recall = _ratio(matrix.true_positive, matrix.true_positive + matrix.false_negative)
    f1 = 0.0 if precision + recall == 0 else round(2 * precision * recall / (precision + recall), 4)

    return {
        "void_precision": precision,
        "void_recall": recall,
        "void_f1": f1,
        "true_positive": float(matrix.true_positive),
        "false_positive": float(matrix.false_positive),
        "false_negative": float(matrix.false_negative),
        "true_negative": float(matrix.true_negative),
        "uncertain": float(matrix.uncertain),
        "errors": float(matrix.errors),
        "items": float(matrix.total),
    }


def score(
    predictions: list[Verdict | None],
    labels: list[bool],
    uncertain_counts_as_void: bool = False,
) -> dict[str, float]:
    return void_metrics(confusion(predictions, labels, uncertain_counts_as_void))

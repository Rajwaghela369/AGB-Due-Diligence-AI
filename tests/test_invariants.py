import pytest
from core.invariants import LabelLeakError, assert_no_labels


def test_clean_columns_pass():
    assert_no_labels(["clause_id", "text", "topics"], where="clauses table")


@pytest.mark.parametrize("leak", ["void", "label_void", "target"])
def test_label_column_raises(leak):
    with pytest.raises(LabelLeakError):
        assert_no_labels(["clause_id", "text", leak], where="clauses table")

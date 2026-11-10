import json

from pipelines.p2_eval.flow import check_gate, write_counts
from pipelines.p2_eval.s1_fetch_agb import FetchResult
from pipelines.p2_eval.s2_normalize import NormalizeResult
from pipelines.p2_eval.s3_filter_scope import ScopeResult
from pipelines.p2_eval.s4_split import SplitResult
from pipelines.p2_eval.s5_publish import PublishResult

SPLITS = {"dev": "train", "held_out": "test"}

# The numbers the design doc fixed, and the run that meets them.
GATE = {
    "scoped_clauses": 523,
    "scoped_void": 39,
    "dev_clauses": 419,
    "dev_void": 31,
    "held_out_clauses": 104,
    "held_out_void": 8,
}


def _scope(clauses=523, void=39):
    return ScopeResult(
        path=None,
        clauses=clauses,
        void=void,
        contracts=89,
        fragments=1,
        foreign_law_clauses=18,
        foreign_law_contracts=[18, 29],
    )


def _split(dev=419, dev_void=31, held=104, held_void=8, tmp_path=None):
    return SplitResult(
        paths={"train": tmp_path or "train.parquet", "test": tmp_path or "test.parquet"},
        counts={"train": dev, "test": held},
        void={"train": dev_void, "test": held_void},
    )


def test_gate_passes_on_the_expected_counts():
    results = check_gate(_scope(), _split(), SPLITS, GATE)

    assert len(results) == 6
    assert all(check["passed"] for check in results)


def test_gate_names_every_mismatch():
    # The fragment dropped instead of flagged, and a void clause lost with it.
    results = check_gate(_scope(clauses=522, void=38), _split(dev=418), SPLITS, GATE)

    failed = {
        check["check"]: (check["got"], check["want"]) for check in results if not check["passed"]
    }
    assert failed == {
        "scoped_clauses": (522, 523),
        "scoped_void": (38, 39),
        "dev_clauses": (418, 419),
    }


def test_counts_report_records_the_flags_and_the_verdict(tmp_path):
    path = tmp_path / "p2_counts.json"
    results = check_gate(_scope(), _split(), SPLITS, GATE)

    write_counts(
        path,
        "abc123",
        FetchResult(
            corpus_path=tmp_path / "c.csv",
            splits_path=tmp_path / "s.parquet",
            commit="9b847807ec38",
            rows=3764,
            assigned=3759,
            unassigned=5,
        ),
        NormalizeResult(path=tmp_path / "n.parquet", rows=3764, fragments=37, with_citations=209),
        _scope(),
        _split(),
        PublishResult("clauses", 523, "agb-de-lw-test-v1", 104, False),
        results,
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["gate_passed"] is True
    assert report["scoped"]["clauses"] == 523
    # Flagged, not dropped - the report is where that stays visible.
    assert report["scoped"]["fragments"] == 1
    assert report["scoped"]["foreign_law_contracts"] == [18, 29]
    assert report["splits"] == {
        "train": {"clauses": 419, "void": 31},
        "test": {"clauses": 104, "void": 8},
    }
    assert report["published"]["dataset_items"] == 104
    assert report["unassigned_corpus_rows"] == 5


def test_counts_report_is_written_even_when_the_gate_fails(tmp_path):
    path = tmp_path / "p2_counts.json"
    results = check_gate(_scope(clauses=500), _split(), SPLITS, GATE)

    write_counts(
        path,
        "abc123",
        FetchResult(tmp_path / "c.csv", tmp_path / "s.parquet", "9b84", 3764, 3759, 5),
        NormalizeResult(tmp_path / "n.parquet", 3764, 37, 209),
        _scope(clauses=500),
        _split(),
        PublishResult("clauses", 0, "agb-de-lw-test-v1", 0, True),
        results,
    )

    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["gate_passed"] is False
    assert report["published"]["table_rows"] == 0

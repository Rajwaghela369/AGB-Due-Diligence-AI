"""The AVV answer key stays auditable and external publication stays opt-in."""

from collections import Counter
from pathlib import Path

import pytest
from core import langfuse_client
from core.config import load_yaml
from core.paths import CONFIGS, GOLD

from pipelines.p2_eval.avv_dataset import (
    OBLIGATIONS,
    build_dataset_items,
    build_gold_set,
    extract_source_clauses,
    upload_dataset,
)
from tests.fixtures.avv_sources import write_avv_sources


def _rows(tmp_path: Path) -> list[dict]:
    paths = write_avv_sources(tmp_path / "raw")
    output = tmp_path / "avv_clauses.jsonl"
    return build_gold_set(
        paths["template"],
        GOLD / "avv" / "annotations.jsonl",
        output,
        paths["template"].parent / "eu_scc_source.json",
    )


def test_gold_set_rebuilds_from_pinned_source_and_manual_annotations(tmp_path):
    paths = write_avv_sources(tmp_path / "raw")
    output = tmp_path / "avv.jsonl"
    rows = build_gold_set(
        paths["template"],
        GOLD / "avv" / "annotations.jsonl",
        output,
        paths["template"].parent / "eu_scc_source.json",
    )

    assert rows == _rows(tmp_path / "expected")


def test_counts_splits_and_complete_three_way_labels_match_config(tmp_path):
    rows = _rows(tmp_path)
    gate = load_yaml(CONFIGS / "eval.yaml")["avv"]["gate"]

    assert len(rows) == gate["clauses"] == 40
    assert Counter(row["split"] for row in rows) == {"dev": 30, "held_out": 10}
    assert sum(bool(row["failed"]) for row in rows) == gate["failed_examples"] == 8
    for row in rows:
        assert tuple(row["labels"]) == OBLIGATIONS
        assert set(row["labels"].values()) <= {"fulfilled", "failed", "silent"}
        assert sorted(row["fulfilled"] + row["failed"] + row["silent"]) == list(OBLIGATIONS)


def test_every_letter_has_positive_and_controlled_negative_coverage(tmp_path):
    rows = _rows(tmp_path)

    fulfilled = Counter(code for row in rows for code in row["fulfilled"])
    failed = Counter(code for row in rows for code in row["failed"])
    assert set(fulfilled) == set(OBLIGATIONS)
    assert failed == Counter({code: 1 for code in OBLIGATIONS})


def test_verbatim_rows_match_source_and_adaptations_are_explicit(tmp_path):
    paths = write_avv_sources(tmp_path / "raw")
    source = {clause.ordinal: clause for clause in extract_source_clauses(paths["template"])}

    for row in _rows(tmp_path / "rows"):
        if row["source"]["verbatim"]:
            assert row["text"] == source[row["source"]["source_ordinal"]].text
            assert row["source"]["adaptation"] is None
        else:
            assert row["source"]["adaptation"].startswith("Counterfactual negative:")
            assert len(row["failed"]) == 1


def test_langfuse_items_contain_only_held_out_labels_in_expected_output(tmp_path):
    items = build_dataset_items(_rows(tmp_path))

    assert len(items) == 10
    assert {item["metadata"]["split"] for item in items} == {"held_out"}
    assert all("obligations" not in item["input"] for item in items)
    assert all(tuple(item["expected_output"]["obligations"]) == OBLIGATIONS for item in items)


def test_langfuse_upload_requires_approval_and_works_with_an_injected_fake(monkeypatch, tmp_path):
    calls: list[tuple[str, dict]] = []

    class FakeLangfuse:
        def create_dataset(self, **kwargs) -> None:
            calls.append(("dataset", kwargs))

        def create_dataset_item(self, **kwargs) -> None:
            calls.append(("item", kwargs))

        def flush(self) -> None:
            calls.append(("flush", {}))

    items = build_dataset_items(_rows(tmp_path))
    with pytest.raises(PermissionError, match="approve-live-upload"):
        upload_dataset(items, "test", approved=False)

    monkeypatch.setattr(langfuse_client, "client", lambda: FakeLangfuse())
    assert upload_dataset(items, "test", approved=True) == 10
    assert Counter(kind for kind, _ in calls) == {"dataset": 1, "item": 10, "flush": 1}

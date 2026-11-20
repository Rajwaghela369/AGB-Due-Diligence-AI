from contextlib import contextmanager

import pandas as pd
import pytest
from core.invariants import LabelLeakError

from pipelines.p2_eval.s5_publish import (
    CLAUSE_COLUMNS,
    build_clause_rows,
    build_dataset_items,
    compose_title,
    publish,
    publish_clauses,
)


def _half(**overrides):
    row = {
        "clause_id": "agb-606",
        "source_id": 606,
        "contract_id": 18,
        "section_no": "8",
        "section_title": "Gewährleistung",
        "text": "Die Haftung für Mängel ist ausgeschlossen.",
        "topics": ["warranty"],
        "subtopics": [],
        "is_fragment": False,
        "foreign_law_choice": True,
        "label_void": True,
    }
    return pd.DataFrame([row | overrides])


@pytest.mark.parametrize(
    ("section_no", "section_title", "expected"),
    [
        ("8", "Gewährleistung", "8 Gewährleistung"),
        ("5", None, "5"),
        (None, "Haftung", "Haftung"),
        (None, None, ""),
    ],
)
def test_compose_title(section_no, section_title, expected):
    assert compose_title(section_no, section_title) == expected


def test_build_clause_rows_publishes_both_halves_without_the_label():
    halves = {"train": _half(clause_id="agb-1"), "test": _half(clause_id="agb-2")}

    rows = build_clause_rows(halves)

    assert list(rows.columns) == list(CLAUSE_COLUMNS)
    assert "label_void" not in rows.columns
    assert sorted(rows.split) == ["test", "train"]
    assert rows.topics.iloc[0] == ["warranty"]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeCursor:
    def __init__(self):
        self.inserted = 0

    def executemany(self, _sql, payload):
        self.inserted = len(payload)


class _FakeConnection:
    """Stands in for Neon: records SQL and reports whatever columns we rig."""

    def __init__(self, columns):
        self.columns = columns
        self.statements: list[str] = []
        self.cursor_obj = _FakeCursor()

    def execute(self, sql, params=None):
        self.statements.append(sql.strip().split("\n")[0])
        if "information_schema.columns" in sql:
            return _FakeResult([(name,) for name in self.columns])
        return _FakeResult([])

    def cursor(self):
        return self.cursor_obj


@contextmanager
def _fake_connection(conn):
    yield conn


def test_publish_clauses_asserts_against_the_table_postgres_reports(monkeypatch):
    # The frame is clean, but the table has a stray label column - exactly the
    # case the frame-level assertion cannot see.
    conn = _FakeConnection(list(CLAUSE_COLUMNS) + ["label_void"])
    monkeypatch.setattr("pipelines.p2_eval.s5_publish.connection", lambda: _fake_connection(conn))
    rows = build_clause_rows({"train": _half()})

    with pytest.raises(LabelLeakError, match="label_void"):
        publish_clauses(rows, "clauses", dry_run=False)


def test_publish_clauses_rebuilds_the_table_in_one_transaction(monkeypatch):
    conn = _FakeConnection(list(CLAUSE_COLUMNS))
    monkeypatch.setattr("pipelines.p2_eval.s5_publish.connection", lambda: _fake_connection(conn))
    rows = build_clause_rows({"train": _half(clause_id="agb-1"), "test": _half(clause_id="agb-2")})

    written = publish_clauses(rows, "clauses", dry_run=False)

    assert written == 2
    assert conn.cursor_obj.inserted == 2
    # Truncate must precede the insert, and both sit inside one connection
    # block so a failure rolls back rather than emptying the table.
    assert any(statement.startswith("truncate") for statement in conn.statements)


def test_build_dataset_items_keeps_the_label_only_in_expected_output():
    items = build_dataset_items(_half(), "test")

    assert len(items) == 1
    item = items[0]
    assert item["id"] == "agb-606"
    assert item["expected_output"] == {"label_void": True}
    # What the agent is handed must not contain the answer.
    assert set(item["input"]) == {"clause_id", "title", "text", "topics"}
    assert "label_void" not in item["input"]
    assert "label_void" not in item["metadata"]
    assert item["metadata"]["foreign_law_choice"] is True


def test_dry_run_asserts_everything_and_writes_nothing():
    halves = {"train": _half(clause_id="agb-1"), "test": _half(clause_id="agb-2")}

    result = publish(
        halves=halves,
        held_out="test",
        table="clauses",
        dataset="agb-de-lw-test-v1",
        dry_run=True,
    )

    assert result.dry_run is True
    assert (result.table_rows, result.dataset_items) == (2, 1)

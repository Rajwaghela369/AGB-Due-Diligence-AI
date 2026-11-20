"""The clause loader, and the third place the label-leak invariant is asserted.

P2 asserts it twice on the write side; `docs/Phase 2 Results.md` promises a
check at read time too. This is where that promise becomes true.
"""

from contextlib import contextmanager

import pytest
from core.invariants import LabelLeakError

from app.agent import clauses

COLUMNS = ("clause_id", "contract_id", "title", "text", "topics", "split")
ROW = ("agb-349", 12, "6 Haftung", "Wir haften nur bei Vorsatz.", ["liability"], "train")


class FakeConnection:
    def __init__(self, columns, rows):
        self.columns = columns
        self.rows = rows
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql
        self.statements.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchall(self):
        if "information_schema.columns" in self._last:
            return [(name,) for name in self.columns]
        return self.rows


@contextmanager
def _connection(conn):
    yield conn


def _patch(monkeypatch, conn):
    monkeypatch.setattr(clauses, "connection", lambda: _connection(conn))


def test_loads_clauses_from_the_published_table(monkeypatch):
    _patch(monkeypatch, FakeConnection(COLUMNS, [ROW]))

    loaded = clauses.load_clauses()

    assert len(loaded) == 1
    assert loaded[0].clause_id == "agb-349"
    assert loaded[0].topics == ["liability"]
    assert loaded[0].split == "train"


def test_a_stray_label_column_stops_the_read(monkeypatch):
    """The agent must never be able to read the answer key, by any route."""
    leaky = FakeConnection((*COLUMNS, "label_void"), [ROW])
    _patch(monkeypatch, leaky)

    with pytest.raises(LabelLeakError, match="label_void"):
        clauses.load_clauses()


def test_the_split_filter_is_parameterised(monkeypatch):
    conn = FakeConnection(COLUMNS, [ROW])
    _patch(monkeypatch, conn)

    clauses.load_clauses(split="train")

    select = [s for s in conn.statements if s.lower().startswith("select clause_id")][0]
    assert "where split = %s" in select.lower()
    assert ("train",) in conn.params


def test_unfiltered_reads_pass_no_parameters(monkeypatch):
    conn = FakeConnection(COLUMNS, [ROW])
    _patch(monkeypatch, conn)

    clauses.load_clauses()

    select = [s for s in conn.statements if s.lower().startswith("select clause_id")][0]
    assert "where" not in select.lower()


def test_results_are_ordered_so_a_slice_is_reproducible(monkeypatch):
    """The v1 slice is taken off the front, so the order must be deterministic."""
    conn = FakeConnection(COLUMNS, [ROW])
    _patch(monkeypatch, conn)

    clauses.load_clauses()

    select = [s for s in conn.statements if s.lower().startswith("select clause_id")][0]
    assert "order by clause_id" in select.lower()

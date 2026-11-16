"""The verdict table's key, its upsert, and what the upsert refuses to touch."""

from contextlib import contextmanager

import pytest
from core.hashing import content_hash, normalise
from core.schemas import VerdictRecord

from app.agent import verdicts

APP_CONFIG = {
    "models": {"judge": {"provider": "gemini", "name": "gemini-decision-1"}},
    "retrieval": {"top_k": 10, "search_depth": 20},
    "agent": {"confidence_uncertain_below": 0.6},
}
FINGERPRINTS = {"void": "aaaaaaaaaaaa", "basis": "bbbbbbbbbbbb", "explain": "cccccccccccc"}

RECORD = VerdictRecord(
    run_id="run-1",
    clause_id="agb-349",
    verdict="potentially_void",
    legal_basis=["bgb-309-7-a"],
    reason_de="Unwirksam nach § 309 Nr. 7.",
    confidence=0.91,
    langfuse_trace_id="trace-abc",
)
HASH = content_hash("Wir haften nur bei Vorsatz.")


class FakeConnection:
    """Records every statement so a test can assert on the SQL actually sent."""

    def __init__(self, row=None):
        self.statements: list[str] = []
        self.params: list[tuple] = []
        self.row = row

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchone(self):
        return self.row


@contextmanager
def _connection(conn):
    yield conn


def _patch(monkeypatch, conn):
    monkeypatch.setattr(verdicts, "connection", lambda: _connection(conn))


def test_save_is_a_single_insert_and_creates_nothing(monkeypatch):
    """At concurrency 8 a CREATE TABLE per clause raced on Postgres for no benefit."""
    conn = FakeConnection()
    _patch(monkeypatch, conn)

    verdicts.save(RECORD, "version-1", HASH)

    assert len(conn.statements) == 1
    assert "create table" not in conn.statements[0].lower()
    assert conn.statements[0].lower().startswith("insert into verdicts")


def test_save_keys_on_the_content_hash_and_version(monkeypatch):
    conn = FakeConnection()
    _patch(monkeypatch, conn)

    verdicts.save(RECORD, "version-1", HASH)

    assert "on conflict (content_hash, agent_version) do nothing" in conn.statements[0].lower()
    # The text hash, not the eval clause id; the version, not the run.
    assert conn.params[0][:2] == (HASH, "version-1")
    assert "agb-349" not in conn.params[0]


def test_there_is_no_lawyer_decision_column_any_more(monkeypatch):
    """A ruling belongs to one review's `decisions`, not to every document sharing the clause."""
    conn = FakeConnection()
    _patch(monkeypatch, conn)

    verdicts.save(RECORD, "version-1", HASH)

    assert "lawyer_decision" not in conn.statements[0].lower()
    assert not hasattr(RECORD, "lawyer_decision")


def test_ensure_table_only_checks_it_does_not_create(monkeypatch):
    conn = FakeConnection(row=(1,))
    _patch(monkeypatch, conn)

    verdicts.ensure_table()

    assert "create table" not in conn.statements[0].lower()


def test_ensure_table_points_at_the_migrations_when_the_schema_is_old(monkeypatch):
    _patch(monkeypatch, FakeConnection(row=None))

    with pytest.raises(RuntimeError, match="app.store.migrate"):
        verdicts.ensure_table()


def test_get_returns_none_when_the_clause_was_never_judged(monkeypatch):
    _patch(monkeypatch, FakeConnection(row=None))

    assert verdicts.get(HASH, "version-1") is None


def test_get_round_trips_a_row(monkeypatch):
    row = (HASH, "version-1", "potentially_void", ["bgb-309-7-a"], "Unwirksam.", 0.91, "trace-abc")
    _patch(monkeypatch, FakeConnection(row=row))

    found = verdicts.get(HASH, "version-1")

    assert found.content_hash == HASH
    assert found.legal_basis == ["bgb-309-7-a"]
    assert found.trace_id == "trace-abc"


def test_agent_version_is_stable_for_the_same_inputs():
    first = verdicts.agent_version(APP_CONFIG, "index-1", FINGERPRINTS)
    second = verdicts.agent_version(APP_CONFIG, "index-1", FINGERPRINTS)

    assert first == second


def test_agent_version_moves_when_a_prompt_moves():
    before = verdicts.agent_version(APP_CONFIG, "index-1", FINGERPRINTS)
    after = verdicts.agent_version(APP_CONFIG, "index-1", {**FINGERPRINTS, "void": "zzzzzzzzzzzz"})

    assert before != after


def test_agent_version_moves_when_the_index_is_rebuilt():
    """The previous build hardcoded "unknown-index", so a rebuild served stale verdicts."""
    before = verdicts.agent_version(APP_CONFIG, "index-1", FINGERPRINTS)
    after = verdicts.agent_version(APP_CONFIG, "index-2", FINGERPRINTS)

    assert before != after


def test_agent_version_moves_when_the_model_or_policy_moves():
    model_changed = {
        **APP_CONFIG,
        "models": {"judge": {"provider": "gemini", "name": "something-else"}},
    }
    policy_changed = {**APP_CONFIG, "retrieval": {"top_k": 15, "search_depth": 20}}
    base = verdicts.agent_version(APP_CONFIG, "index-1", FINGERPRINTS)

    assert verdicts.agent_version(model_changed, "index-1", FINGERPRINTS) != base
    assert verdicts.agent_version(policy_changed, "index-1", FINGERPRINTS) != base


def test_content_hash_ignores_whitespace_and_unicode_form():
    assert content_hash("Wir  haften\nnur") == content_hash("Wir haften nur")
    # NFC: a composed and a decomposed umlaut are the same clause.
    assert content_hash("Gewährleistung") == content_hash("Gewährleistung")


def test_content_hash_separates_clauses_that_differ_in_a_decisive_word():
    """Over-normalising here would merge clauses with different verdicts."""
    narrow = "Die Haftung ist auf Vorsatz beschränkt."
    wider = "Die Haftung ist auf Vorsatz und grobe Fahrlässigkeit beschränkt."

    assert content_hash(narrow) != content_hash(wider)


def test_normalise_collapses_every_run_of_whitespace():
    assert normalise("  a \t\n b  ") == "a b"

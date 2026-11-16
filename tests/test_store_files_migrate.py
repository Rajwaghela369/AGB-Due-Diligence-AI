"""LocalFileStore on disk, and the migration runner against a fake connection."""

from contextlib import contextmanager

import pytest

from app.store import migrate
from app.store.files import LocalFileStore

# --- LocalFileStore ---------------------------------------------------------------


def test_files_are_written_under_the_root_and_read_back(tmp_path):
    files = LocalFileStore(tmp_path / "files")

    files.put("documents/abc.pdf", b"%PDF")

    assert (tmp_path / "files" / "documents" / "abc.pdf").read_bytes() == b"%PDF"
    assert files.get("documents/abc.pdf") == b"%PDF"
    assert files.exists("documents/abc.pdf") and not files.exists("documents/nope.pdf")


def test_put_leaves_no_temp_files_and_never_rewrites(tmp_path):
    files = LocalFileStore(tmp_path)
    files.put("a.bin", b"one")
    files.put("a.bin", b"two")

    assert files.get("a.bin") == b"one"
    assert [p.name for p in tmp_path.iterdir()] == ["a.bin"]


def test_a_failed_write_leaves_neither_the_file_nor_a_temp(tmp_path, monkeypatch):
    files = LocalFileStore(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("app.store.files.os.link", boom)
    with pytest.raises(OSError):
        files.put("a.bin", b"x")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["../escape", "/etc/passwd", "", "a/../../b"])
def test_keys_cannot_leave_the_root(tmp_path, key):
    files = LocalFileStore(tmp_path / "root")

    with pytest.raises(ValueError):
        files.put(key, b"x")


def test_a_missing_key_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalFileStore(tmp_path).get("missing")


# --- migrate -----------------------------------------------------------------------


class FakeDb:
    """Just enough of a Postgres to run `apply_pending`: it tracks recorded versions."""

    def __init__(self):
        self.recorded: set[str] = set()
        self.script_runs: list[str] = []
        self.transactions = 0
        self._last = None

    @contextmanager
    def connect(self):
        self.transactions += 1
        yield self

    def execute(self, sql, params=None):
        text = " ".join(sql.split()).lower()
        self._last = None
        if text.startswith("select 1 from schema_migrations"):
            self._last = (1,) if params[0] in self.recorded else None
        elif text.startswith("insert into schema_migrations"):
            self.recorded.add(params[0])
        elif "create table if not exists schema_migrations" in text or "advisory" in text:
            pass
        else:
            self.script_runs.append(text)
        return self

    def fetchone(self):
        return self._last


def test_migrations_apply_in_order_and_are_recorded():
    db = FakeDb()

    applied = migrate.apply_pending(db.connect)

    assert applied == [f.stem for f in migrate.migration_files()]
    assert db.recorded == set(applied)
    assert len(db.script_runs) == len(applied)


def test_migrate_is_idempotent():
    db = FakeDb()
    migrate.apply_pending(db.connect)
    runs = len(db.script_runs)

    assert migrate.apply_pending(db.connect) == []
    assert len(db.script_runs) == runs


def test_each_migration_is_its_own_transaction(tmp_path):
    (tmp_path / "001_a.sql").write_text("select 1")
    (tmp_path / "002_b.sql").write_text("select 2")
    db = FakeDb()

    migrate.apply_pending(db.connect, tmp_path)

    assert db.transactions == 3  # bookkeeping + one per file


def test_migration_files_are_numbered_without_gaps():
    numbers = [int(f.name.split("_")[0]) for f in migrate.migration_files()]

    assert numbers == list(range(1, len(numbers) + 1))


def test_a_badly_named_file_is_rejected(tmp_path):
    (tmp_path / "add_stuff.sql").write_text("select 1")

    with pytest.raises(ValueError, match="NNN"):
        migrate.migration_files(tmp_path)


def test_the_first_migration_keeps_the_legacy_eval_verdicts_and_leaves_clauses_alone():
    sql = (migrate.MIGRATIONS / "001_app_tables.sql").read_text().lower()

    assert "rename to verdicts_legacy_eval" in sql
    assert "create table verdicts (" in sql and "primary key (content_hash, agent_version)" in sql
    assert "lawyer_decision" not in sql.split("create table verdicts (")[1]
    assert "table clauses" not in sql  # the answer key is untouched


def test_concurrent_puts_of_one_key_keep_the_first_writer(tmp_path):
    import threading

    files = LocalFileStore(tmp_path)
    barrier = threading.Barrier(8)

    def work(i):
        barrier.wait()
        files.put("k.bin", f"writer-{i}".encode())

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    first = files.get("k.bin")
    assert first.startswith(b"writer-")
    files.put("k.bin", b"late")
    assert files.get("k.bin") == first
    assert [p.name for p in tmp_path.iterdir()] == ["k.bin"]


def test_bookkeeping_table_is_created_only_after_taking_the_lock():
    db = FakeDb()
    order = []
    orig = db.execute
    db.execute = lambda sql, params=None: (
        order.append(" ".join(sql.split())[:80]),
        orig(sql, params),
    )[1]

    migrate.apply_pending(db.connect)

    assert "advisory" in order[0] and "schema_migrations" in order[1]


def test_legacy_verdicts_are_carried_over_deterministically():
    sql = (migrate.MIGRATIONS / "001_app_tables.sql").read_text().lower()

    assert "from verdicts_legacy_eval" in sql and "join clauses c" in sql
    assert "distinct on (c.content_hash, l.agent_version)" in sql
    assert "l.confidence desc, l.run_id, l.clause_id" in sql

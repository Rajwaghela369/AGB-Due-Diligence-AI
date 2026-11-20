"""The Postgres deletion under concurrency (9.B). Runs only with `TEST_DATABASE_URL` set.

Point it at a throwaway database (a Neon branch, or a local Postgres): the migrations are
applied once, every test uses fresh ids, and none of them assumes the tables are empty.

What only a real database can show: the `set local agb.erasure` path through the
append-only trigger (test_store.py shows it still refuses everyone else), the rollback of
a failed deletion, the content lock that serialises two deletions sharing a clause and a
source file, a deletion waiting for an ingest that reuses a clause hash, the erasure
backoff in SQL, the blob re-check, the review-orphan sweep, and the LangGraph Postgres
saver erasing every thread of a review.
"""

import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from app.domain import (
    ClauseSpan,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Review,
    checkpoint_key,
)
from tests.test_deletion import seed

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")


@contextmanager
def _connect():
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


def _scalar(sql: str, params: tuple = ()):
    with _connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


@pytest.fixture(scope="module")
def _migrated():
    from app.store.migrate import apply_pending

    apply_pending(_connect)


@pytest.fixture
def store(tmp_path, _migrated):
    from app.store.files import LocalFileStore
    from app.store.postgres import postgres_store

    return postgres_store(LocalFileStore(tmp_path), _connect)


def uid() -> str:
    return uuid.uuid4().hex


def _document(document_id: str, source_hash: str | None = None) -> Document:
    return Document(
        document_id=document_id,
        filename="agb.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash=document_id,
        source_hash=source_hash,
    )


def _save(store, document_id: str, hashes: list[str], source_hash: str | None = None) -> None:
    clauses = [
        ClauseSpan(position=position, text=content_hash, content_hash=content_hash)
        for position, content_hash in enumerate(hashes)
    ]
    store.documents.save(
        _document(document_id, source_hash), clauses, {c.content_hash: c.text for c in clauses}
    )
    for clause in clauses:
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version="agent",
                verdict="valid",
                confidence=0.9,
            )
        )


def _pending(store, keys: set[str]) -> list:
    return [task for task in store.deletion.pending_erasures(1_000_000) if task.key in keys]


def test_a_document_deletion_removes_every_review_row_through_the_trigger(store) -> None:
    document_id, shared = f"doc-{uid()}", f"shared-{uid()}"
    seed(store, document_id, [shared, f"unique-{uid()}"], trace=f"trace-{uid()}")
    review_id = f"review-{document_id}"
    store.events.append(review_id, "RUN_FINISHED", {})
    store.decisions.put(
        Decision(
            review_id=review_id,
            position=0,
            action="override",
            corrected_verdict="potentially_void",
            model_verdict="valid",
            note="Mandant Müller",
        ),
        agent_version="agent",
    )
    store.feedback.put(
        Feedback(
            feedback_id=f"feedback-{uid()}",
            review_id=review_id,
            position=0,
            agent_version="agent",
            vote="up",
            observations={"judge": "observation"},
        )
    )
    store.caches.put_summary_text(f"set-{uid()}", "model", "Absatz.", review_id=review_id)

    report = store.deletion.delete_document(document_id, deleted_by=None, reason="user")

    for table in (
        "reviews",
        "review_clauses",
        "run_events",
        "decisions",
        "decision_events",
        "feedback",
        "summary_texts",
    ):
        count = _scalar(f"select count(*) from {table} where review_id = %s", (review_id,))
        assert count == 0, table
    assert report.removed["decision_events"] == 1 and report.removed["summary_texts"] == 1
    assert report.removed["clause_texts"] == 2
    targets = {(task.target, task.key) for task in report.erasures}
    assert ("checkpoint", checkpoint_key(review_id)) in targets
    assert ("blob", f"{document_id}.pdf") in targets
    assert any(target == "dataset_item" for target, _key in targets)


def test_a_failed_deletion_rolls_back_completely(store, monkeypatch) -> None:
    from app.store.deletion import PostgresDeletionRepo

    document_id = f"doc-{uid()}"
    seed(store, document_id, [f"only-{uid()}"])
    store.decisions.put(
        Decision(
            review_id=f"review-{document_id}",
            position=0,
            action="accept",
            model_verdict="valid",
        ),
        agent_version="agent",
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("crash before the tombstone")

    monkeypatch.setattr(PostgresDeletionRepo, "_insert_tombstone", staticmethod(explode))
    with pytest.raises(RuntimeError):
        store.deletion.delete_document(document_id, deleted_by=None, reason="user")

    assert store.documents.get(document_id) is not None
    assert store.reviews.get(f"review-{document_id}") is not None
    assert store.decisions.history(f"review-{document_id}")
    assert store.deletion.tombstones(document_id) == []


def test_two_concurrent_deletions_sharing_a_clause_and_a_file_leak_nothing(store) -> None:
    for _round in range(5):
        shared, source = f"shared-{uid()}", uid()
        first, second = f"a-{uid()}", f"b-{uid()}"
        _save(store, first, [shared, f"u-{uid()}"], source)
        _save(store, second, [shared], source)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def delete(document_id: str, barrier=barrier, errors=errors) -> None:
            try:
                barrier.wait(timeout=10)
                store.deletion.delete_document(document_id, deleted_by=None, reason="user")
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=delete, args=(d,)) for d in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert errors == []
        for table in ("clause_texts", "verdicts"):
            count = _scalar(f"select count(*) from {table} where content_hash = %s", (shared,))
            assert count == 0, table
        # the blob was queued by whichever deletion ran second, exactly once
        blob_tasks = _scalar(
            "select count(*) from erasure_tasks where target = 'blob' and key = %s",
            (f"{source}.pdf",),
        )
        assert blob_tasks == 1


def test_a_deletion_waits_for_an_ingest_that_reuses_a_clause_hash(store) -> None:
    from app.store.deletion import CONTENT_LOCK

    shared, unique = f"shared-{uid()}", f"unique-{uid()}"
    first, newcomer = f"a-{uid()}", f"c-{uid()}"
    _save(store, first, [shared, unique])
    finished = threading.Event()

    with _connect() as ingest:  # an ingest in flight: rows written, not committed
        ingest.execute("select pg_advisory_xact_lock_shared(%s)", (CONTENT_LOCK,))
        ingest.execute(
            "insert into documents (document_id, filename, source_type, parser_version,"
            " splitter_version, text_hash, owner_id, source_hash)"
            " values (%s, 'c.pdf', 'pdf', 'p1', 's1', %s, 'local', %s)",
            (newcomer, newcomer, newcomer),
        )
        ingest.execute(
            "insert into document_clauses (document_id, position, content_hash) values (%s, 0, %s)",
            (newcomer, shared),
        )

        def delete() -> None:
            store.deletion.delete_document(first, deleted_by=None, reason="user")
            finished.set()

        worker = threading.Thread(target=delete)
        worker.start()
        assert not finished.wait(0.5), "the deletion did not wait for the ingest"
    # the ingest committed on leaving the block
    worker.join(timeout=30)
    assert finished.is_set()

    assert _scalar("select count(*) from clause_texts where content_hash = %s", (shared,)) == 1
    assert store.verdicts.get(shared, "agent") is not None  # the newcomer's verdict stays
    assert store.verdicts.get(unique, "agent") is None


def test_failed_erasures_back_off_and_newer_tasks_come_first(store) -> None:
    stuck, fresh = f"stuck-{uid()}", f"fresh-{uid()}"
    (task,) = store.deletion.queue_erasures([("trace", stuck)])
    store.deletion.finish_erasure(task.task_id, "Langfuse is down")
    store.deletion.queue_erasures([("trace", fresh)])

    (failed,) = _pending(store, {stuck})
    assert failed.attempts == 1
    assert failed.next_attempt_at - failed.requested_at >= timedelta(seconds=50)
    due = {task.key for task in store.deletion.pending_erasures(1_000_000, due=True)}
    assert fresh in due and stuck not in due
    ordered = [task.key for task in store.deletion.pending_erasures(1_000_000)]
    assert ordered.index(fresh) < ordered.index(stuck)

    store.deletion.finish_erasure(failed.task_id, "still down")
    (again,) = _pending(store, {stuck})
    assert again.next_attempt_at - failed.next_attempt_at >= timedelta(seconds=50)


def test_a_blob_erasure_rechecks_the_references_when_it_runs(store) -> None:
    source = uid()
    _save(store, f"doc-{uid()}", [f"h-{uid()}"], source)
    deleted: list[str] = []

    def delete(key: str) -> bool:
        deleted.append(key)
        return True

    assert store.deletion.erase_blob(f"{source}.pdf", delete) is False
    assert deleted == []
    assert store.deletion.erase_blob(f"{uid()}.pdf", delete) is True
    assert len(deleted) == 1


def test_a_review_of_a_just_deleted_document_is_refused(store) -> None:
    document_id = f"doc-{uid()}"
    _save(store, document_id, [f"h-{uid()}"])
    store.deletion.delete_document(document_id, deleted_by=None, reason="user")

    with pytest.raises(KeyError):
        store.reviews.create(
            Review(
                review_id=f"r-{uid()}",
                document_id=document_id,
                agent_version="agent",
                scope=["liability"],
            )
        )
    # a document that never existed keeps the old behaviour (no tombstone, no refusal)
    created = store.reviews.create(
        Review(
            review_id=f"r-{uid()}",
            document_id=f"never-{uid()}",
            agent_version="agent",
            scope=["liability"],
        )
    )
    assert created is not None


def test_the_sweep_removes_review_orphans_legacy_summaries_and_queues_old_blobs(store) -> None:
    gone, young = f"gone-{uid()}", f"young-{uid()}"
    old = datetime.now(UTC) - timedelta(hours=2)
    store.decisions.put(
        Decision(
            review_id=gone, position=0, action="accept", model_verdict="valid", decided_at=old
        ),
        agent_version="agent",
    )
    store.decisions.put(
        Decision(review_id=young, position=0, action="accept", model_verdict="valid"),
        agent_version="agent",
    )
    store.feedback.put(
        Feedback(
            feedback_id=f"f-{uid()}",
            review_id=gone,
            position=0,
            agent_version="agent",
            vote="up",
            created_at=old,
        )
    )
    legacy, owned = f"legacy-{uid()}", f"owned-{uid()}"
    with _connect() as conn:
        conn.execute(
            "insert into run_events (review_id, seq, type, payload, created_at)"
            " values (%s, 1, 'CUSTOM', '{}', %s)",
            (gone, old),
        )
        conn.execute(
            "insert into summary_texts (decision_set_hash, model, text, review_id, created_at)"
            " values (%s, 'm', 'Alt.', null, %s), (%s, 'm', 'Weg.', %s, %s)",
            (legacy, datetime.now(UTC) - timedelta(days=40), owned, gone, old),
        )
    source = uid()
    _save(store, f"doc-{uid()}", [f"h-{uid()}"], source)
    orphan = f"orphan-{uid()}.pdf"

    removed = store.deletion.sweep_orphans(
        blobs=[(f"{source}.pdf", old), (orphan, old), (f"new-{uid()}.pdf", datetime.now(UTC))],
        summaries_before=datetime.now(UTC) - timedelta(days=30),
    )

    for table in ("decisions", "decision_events", "feedback", "run_events"):
        assert _scalar(f"select count(*) from {table} where review_id = %s", (gone,)) == 0, table
    assert _scalar("select count(*) from decisions where review_id = %s", (young,)) == 1
    for key in (legacy, owned):
        assert (
            _scalar("select count(*) from summary_texts where decision_set_hash = %s", (key,)) == 0
        )
    assert removed["summary_texts"] >= 2 and removed["blobs"] >= 1
    assert [(task.target, task.key) for task in _pending(store, {orphan, f"{source}.pdf"})] == [
        ("blob", orphan)
    ]
    assert any(t.kind == "sweep" for t in store.deletion.tombstones("orphans"))


def test_the_postgres_saver_erases_every_thread_of_a_review() -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    from app.agent.run import erase_threads
    from app.review.checkpoint import postgres_checkpointer

    checkpointer = postgres_checkpointer(DATABASE_URL, max_size=2)
    try:
        saver = checkpointer.saver
        review = f"r_{uid()}"  # the underscore must not act as a LIKE wildcard
        threads = [f"{review}:0", f"{review}:3", f"{review}0:0", f"{review.replace('_', 'x')}:0"]
        for thread in threads:
            config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
            saver.put(config, empty_checkpoint(), {}, {})

        assert erase_threads(saver, checkpoint_key(review)) == 2

        def alive(thread: str) -> bool:
            return saver.get_tuple({"configurable": {"thread_id": thread}}) is not None

        assert [alive(thread) for thread in threads] == [False, False, True, True]
    finally:
        checkpointer.close()


def test_deleting_a_waiting_review_erases_its_postgres_checkpoint(monkeypatch) -> None:
    from app.domain import default_principal
    from app.review.checkpoint import postgres_checkpointer
    from app.store.memory import memory_store
    from tests.test_interrupt import Provider, _checkpoint, _judge, _review_one

    checkpointer = postgres_checkpointer(DATABASE_URL, max_size=2)
    try:
        saver = checkpointer.saver
        service, review, _events = _review_one(
            memory_store(), _judge(monkeypatch, Provider(), saver)
        )
        assert _checkpoint(saver, f"{review.review_id}:0") is not None

        report = service.delete_document(review.document_id, default_principal())

        task = next(task for task in report.erasures if task.target == "checkpoint")
        assert task.done_at is not None
        assert _checkpoint(saver, f"{review.review_id}:0") is None
        service.shutdown(timeout=10)
    finally:
        checkpointer.close()

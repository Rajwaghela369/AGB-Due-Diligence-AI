"""Deletion removes owned state, preserves shared state and drains the external outbox."""

import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, Document, Review, default_principal
from app.review.deletion import delete_document, delete_review, process_erasures
from app.review.feedback import FeedbackService
from app.review.interfaces import DeletionBlocked
from app.review.service import build_service
from app.store.memory import memory_store
from tests.test_review_runner import FakeJudge, StubLimiter


class Endpoint:
    def __init__(self, deleted: list[tuple[str, str]], kind: str) -> None:
        self.deleted = deleted
        self.kind = kind

    def delete(self, key: str) -> None:
        self.deleted.append((self.kind, key))


def service(store, *, tracer=True):
    deleted: list[tuple[str, str]] = []
    api = SimpleNamespace(
        trace=Endpoint(deleted, "trace"),
        scores=Endpoint(deleted, "score"),
        dataset_items=Endpoint(deleted, "dataset_item"),
    )
    forgotten: list[str] = []
    judge = SimpleNamespace(forget=forgotten.append)
    result = SimpleNamespace(
        store=store,
        tracer=SimpleNamespace(api=api) if tracer else None,
        judge=judge,
    )
    result.process_erasures = lambda limit=100: process_erasures(result, limit)
    return result, deleted, forgotten


def seed(store, document_id: str, hashes: list[str], *, trace: str | None = None) -> None:
    clauses = [
        ClauseSpan(position=position, text=content_hash, content_hash=content_hash)
        for position, content_hash in enumerate(hashes)
    ]
    store.documents.save(
        Document(
            document_id=document_id,
            filename=f"{document_id}.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash=document_id,
        ),
        clauses,
        {clause.content_hash: clause.text for clause in clauses},
    )
    store.files.put(f"{document_id}.pdf", b"pdf")
    for clause in clauses:
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version="agent",
                verdict="valid",
                confidence=0.9,
                trace_id=f"verdict-{clause.content_hash}",
            )
        )
    review = Review(
        review_id=f"review-{document_id}",
        document_id=document_id,
        agent_version="agent",
        scope=["liability"],
        status="finished",
        trace_id=trace,
    )
    store.reviews.create(review)
    store.reviews.set_clause_statuses(
        review.review_id, {position: ("done", None) for position in range(len(clauses))}
    )


def test_delete_document_preserves_shared_content_and_finishes_external_tasks() -> None:
    store = memory_store()
    seed(store, "one", ["shared", "only-one"], trace="review-trace")
    seed(store, "two", ["shared", "only-two"])
    holder, deleted, forgotten = service(store)

    planned = delete_document(holder, "one", default_principal(), dry_run=True)
    assert store.documents.get("one") is not None
    assert planned.dry_run and planned.tombstone is None

    report = delete_document(holder, "one", default_principal())

    assert store.documents.get("one") is None
    assert store.documents.get("two") is not None
    assert set(store.documents._texts) == {"shared", "only-two"}
    assert report.kept_shared["clause_texts"] == 1
    assert report.tombstone is not None and report.tombstone.subject_id == "one"
    assert all(task.done_at is not None for task in report.erasures)
    assert ("trace", "review-trace") in deleted
    assert ("trace", "verdict-only-one") in deleted
    assert forgotten == ["review-one:*"]
    assert not store.files.exists("one.pdf") and store.files.exists("two.pdf")


def test_active_review_blocks_even_a_dry_run() -> None:
    store = memory_store()
    seed(store, "one", ["a"])
    store.reviews._reviews["review-one"].status = "running"
    holder, _deleted, _forgotten = service(store)

    with pytest.raises(DeletionBlocked) as caught:
        delete_document(holder, "one", default_principal(), dry_run=True)

    assert caught.value.review_ids == ["review-one"]
    assert store.documents.get("one") is not None


def test_failed_external_erasure_stays_pending() -> None:
    store = memory_store()
    store.deletion.queue_erasures([("trace", "trace-1")])
    holder, _deleted, _forgotten = service(store, tracer=False)

    assert process_erasures(holder) == (0, 1)
    task = store.deletion.pending_erasures()[0]
    assert task.attempts == 1 and "not configured" in task.last_error


def test_delete_route_supports_dry_run_then_deletes() -> None:
    store = memory_store()
    seed(store, "one", ["a"])
    review_service = build_service(store, FakeJudge(), StubLimiter())
    application = create_app(
        store=store,
        service=review_service,
        statutes={},
        feedback=FeedbackService(store, None),
        settings={},
    )

    with TestClient(application) as client:
        planned = client.delete("/api/v1/documents/one?dry_run=true")
        deleted = client.delete("/api/v1/documents/one")
        missing = client.delete("/api/v1/documents/one")

    assert planned.status_code == 200 and planned.json()["report"]["dry_run"] is True
    assert deleted.status_code == 200 and deleted.json()["report"]["tombstone"] is not None
    assert missing.status_code == 404


DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@contextmanager
def _connect():
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")
def test_postgres_reference_counting_matches_memory(tmp_path) -> None:
    from app.store.files import LocalFileStore
    from app.store.migrate import apply_pending
    from app.store.postgres import postgres_store

    apply_pending(_connect)
    store = postgres_store(LocalFileStore(tmp_path), _connect)
    suffix = uuid.uuid4().hex
    first_id, second_id = f"first-{suffix}", f"second-{suffix}"
    shared, unique = f"shared-{suffix}", f"unique-{suffix}"
    seed(store, first_id, [shared, unique])
    seed(store, second_id, [shared])

    report = store.deletion.delete_document(first_id, deleted_by=None, reason="user")

    assert report.removed["clause_texts"] == 1
    assert report.kept_shared["clause_texts"] == 1
    assert store.verdicts.get(shared, "agent") is not None
    assert store.verdicts.get(unique, "agent") is None
    store.deletion.delete_document(second_id, deleted_by=None, reason="user")


def test_delete_review_keeps_the_document_and_its_verdicts() -> None:
    store = memory_store()
    seed(store, "one", ["a"], trace="review-trace")
    holder, deleted, forgotten = service(store)

    report = delete_review(holder, "review-one", default_principal())

    assert store.reviews.get("review-one") is None
    assert store.documents.get("one") is not None and store.verdicts.get("a", "agent")
    assert report.removed["reviews"] == 1 and report.removed["clause_texts"] == 0
    assert ("trace", "review-trace") in deleted and ("trace", "verdict-a") not in deleted
    assert forgotten == ["review-one:*"]
    assert store.deletion.tombstones("review-one")[0].kind == "review"


def test_a_shared_source_file_keeps_its_blob_until_the_last_document_goes() -> None:
    store = memory_store()
    holder, _deleted, _forgotten = service(store)
    for document_id in ("mine", "theirs"):
        store.documents.save(
            Document(
                document_id=document_id,
                filename="agb.pdf",
                source_type="pdf",
                parser_version="p1",
                splitter_version="s1",
                text_hash="same",
                source_hash="same-bytes",
                owner_id=document_id,
            ),
            [],
            {},
        )
    store.files.put("same-bytes.pdf", b"pdf")

    first = delete_document(holder, "mine", default_principal("mine"))
    assert store.files.exists("same-bytes.pdf")
    assert all(task.target != "blob" for task in first.erasures)
    delete_document(holder, "theirs", default_principal("theirs"))
    assert not store.files.exists("same-bytes.pdf")


def test_tombstone_records_who_and_counts_but_no_content() -> None:
    store = memory_store()
    seed(store, "one", ["secret clause"])
    holder, _deleted, _forgotten = service(store)

    report = delete_document(holder, "one", default_principal())

    tombstone = report.tombstone.model_dump_json()
    assert "secret clause" not in tombstone and "one.pdf" not in tombstone
    assert report.tombstone.deleted_by == "local" and report.tombstone.reason == "user"
    assert report.tombstone.removed["documents"] == 1


# --- review fixes -------------------------------------------------------------------------


def test_another_owners_document_is_unknown_even_to_a_dry_run() -> None:
    store = memory_store()
    seed(store, "one", ["a"])  # owned by the default owner, "local"
    holder, _deleted, _forgotten = service(store)

    for dry_run in (True, False):
        with pytest.raises(KeyError):
            delete_document(holder, "one", default_principal("someone-else"), dry_run=dry_run)
    with pytest.raises(KeyError):
        delete_review(holder, "review-one", default_principal("someone-else"))
    assert store.documents.get("one") is not None and store.reviews.get("review-one")


def test_a_validation_error_inside_the_repo_is_not_reported_as_blocked(monkeypatch) -> None:
    store = memory_store()
    seed(store, "one", ["a"])
    holder, _deleted, _forgotten = service(store)

    def broken(_document_id):
        raise ValueError("corrupt row")  # e.g. pydantic's ValidationError

    monkeypatch.setattr(store.deletion, "plan_document", broken)
    with pytest.raises(ValueError, match="corrupt row") as caught:
        delete_document(holder, "one", default_principal(), dry_run=True)
    assert not isinstance(caught.value, DeletionBlocked)


def test_failing_tasks_back_off_and_never_block_newer_ones() -> None:
    store = memory_store()
    seed(store, "one", ["a"], trace="review-trace")
    # More always-failing tasks than one batch, queued before the deletion.
    store.deletion.queue_erasures([("trace", f"stuck-{n}") for n in range(150)])
    holder, deleted, forgotten = service(store)

    def refuse(key: str) -> None:
        if key.startswith("stuck-"):
            raise RuntimeError("Langfuse is down")
        deleted.append(("trace", key))

    holder.tracer.api.trace.delete = refuse

    # The deletion runs its own tasks first, however long the failing queue is.
    report = delete_document(holder, "one", default_principal())
    assert ("trace", "review-trace") in deleted and forgotten == ["review-one:*"]
    assert all(task.done_at is not None for task in report.erasures)

    # The worker: a failure moves its task behind the backoff, so the next batch is new.
    assert process_erasures(holder, 100) == (0, 100)
    assert process_erasures(holder, 100) == (0, 50)
    assert process_erasures(holder, 100) == (0, 0)  # nothing due until the backoff passes
    store.deletion.queue_erasures([("trace", "fresh")])
    assert process_erasures(holder, 100) == (1, 0)
    pending = store.deletion.pending_erasures(1000)
    assert len(pending) == 150 and all(task.next_attempt_at > task.requested_at for task in pending)


def test_erasure_backoff_doubles_up_to_a_day() -> None:
    from datetime import timedelta

    from app.store.deletion import erasure_backoff

    assert [erasure_backoff(n) for n in (1, 2, 3)] == [
        timedelta(minutes=1),
        timedelta(minutes=2),
        timedelta(minutes=4),
    ]
    assert erasure_backoff(12) == erasure_backoff(500) == timedelta(hours=24)


def test_a_blob_used_again_before_its_erasure_runs_is_kept() -> None:
    store = memory_store()
    seed(store, "one", ["a"])
    store.deletion.queue_erasures([("blob", "again.pdf")])
    store.files.put("again.pdf", b"pdf")
    # Before the worker runs, someone uploads the same bytes: a document names the key.
    store.documents.save(
        Document(
            document_id="again",
            filename="again.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash="again",
            source_hash="again",
        ),
        [],
        {},
    )
    holder, _deleted, _forgotten = service(store)

    assert process_erasures(holder) == (1, 0)
    assert store.files.exists("again.pdf")
    assert store.deletion.pending_erasures() == []


def test_ingest_puts_the_blob_back_if_an_erasure_removed_it_during_the_save() -> None:
    from app.ingest import ingest_text

    store = memory_store()
    text = "1. Es gilt deutsches Recht."
    import hashlib

    key = f"{hashlib.sha256(text.encode()).hexdigest()}.txt"
    store.deletion.queue_erasures([("blob", key)])  # left by an earlier deletion
    holder, _deleted, _forgotten = service(store)
    save = store.documents.save

    def save_after_the_worker(document, clauses, texts):
        process_erasures(holder)  # the worker wins the race: put was a no-op, blob gone
        assert not store.files.exists(key)
        save(document, clauses, texts)

    store.files.put(key, text.encode())
    store.documents.save = save_after_the_worker
    ingest_text(text, "agb.txt", store=store)

    assert store.files.get(key) == text.encode()


def test_summary_paragraphs_go_with_their_review_and_old_unowned_ones_with_the_sweep() -> None:
    from datetime import UTC, datetime, timedelta

    store = memory_store()
    seed(store, "one", ["a"])
    seed(store, "two", ["b"])
    store.caches.put_summary_text("set-one", "model", "Absatz eins.", review_id="review-one")
    store.caches.put_summary_text("set-two", "model", "Absatz zwei.", review_id="review-two")
    store.caches.put_summary_text("legacy", "model", "Alt.")  # before Phase 9: no review id
    holder, _deleted, _forgotten = service(store)

    report = delete_review(holder, "review-one", default_principal())
    assert report.removed["summary_texts"] == 1
    assert store.caches.get_summary_text("set-one", "model") is None

    report = delete_document(holder, "two", default_principal())
    assert report.removed["summary_texts"] == 1
    assert store.caches.get_summary_text("set-two", "model") is None

    # The legacy row stays until it is older than the reviews period.
    store.deletion.sweep_orphans(summaries_before=datetime.now(UTC) - timedelta(days=1))
    assert store.caches.get_summary_text("legacy", "model") == "Alt."
    removed = store.deletion.sweep_orphans(summaries_before=datetime.now(UTC) + timedelta(1))
    assert removed["summary_texts"] == 1
    assert store.caches.get_summary_text("legacy", "model") is None


def test_the_sweep_removes_rows_written_for_a_review_that_is_gone() -> None:
    from datetime import UTC, datetime, timedelta

    from app.domain import Decision, Feedback

    store = memory_store()
    old = datetime.now(UTC) - timedelta(hours=2)
    # A decision, feedback and an event that lost the race with their review's deletion.
    store.decisions.put(
        Decision(
            review_id="gone",
            position=0,
            action="accept",
            model_verdict="valid",
            decided_at=old,
        ),
        agent_version="agent",
    )
    store.feedback.put(
        Feedback(
            feedback_id="late",
            review_id="gone",
            position=0,
            agent_version="agent",
            vote="up",
            created_at=old,
        )
    )
    store.events.append("gone", "CUSTOM", {})
    store.events._events["gone"][0].created_at = old
    store.caches.put_summary_text("set", "model", "Absatz.", review_id="gone")
    store.caches._summary_created[("set", "model")] = old
    # A young one may be a write in flight: it stays.
    store.decisions.put(
        Decision(review_id="young", position=0, action="accept", model_verdict="valid"),
        agent_version="agent",
    )

    removed = store.deletion.sweep_orphans()

    assert removed["decisions"] == 1 and removed["decision_events"] == 1
    assert removed["feedback"] == 1 and removed["run_events"] == 1
    assert removed["summary_texts"] == 1
    assert store.decisions.for_review("gone") == {} and store.decisions.for_review("young")
    tombstone = store.deletion.tombstones("orphans")[0]
    assert tombstone.kind == "sweep" and tombstone.reason == "orphan_sweep"
    assert tombstone.removed["decisions"] == 1


def test_the_sweep_queues_old_unreferenced_blobs_from_the_store_listing() -> None:
    from datetime import UTC, datetime, timedelta

    store = memory_store()
    seed(store, "one", ["a"])
    old = datetime.now(UTC) - timedelta(hours=2)
    listing = [
        ("one.pdf", old),  # referenced
        ("orphan.pdf", old),  # an upload that never got its document
        ("young.pdf", datetime.now(UTC)),  # may be an upload in flight
    ]

    removed = store.deletion.sweep_orphans(blobs=listing)

    assert removed["blobs"] == 1
    assert [(t.target, t.key) for t in store.deletion.pending_erasures()] == [
        ("blob", "orphan.pdf")
    ]


def test_the_real_judge_erases_every_checkpoint_thread_of_a_review(monkeypatch) -> None:
    from app.review.checkpoint import memory_checkpointer
    from tests.test_interrupt import Provider, _checkpoint, _judge, _review_one

    store = memory_store()
    saver = memory_checkpointer().saver
    judge = _judge(monkeypatch, Provider(), saver)
    review_service, review, _events = _review_one(store, judge)
    assert review.status == "awaiting_decisions"
    thread = f"{review.review_id}:0"
    assert _checkpoint(saver, thread) is not None
    # A thread of another review whose id starts the same must survive.
    other = f"{review.review_id}0:0"
    saver.put(
        {"configurable": {"thread_id": other, "checkpoint_ns": ""}},
        _checkpoint(saver, thread).checkpoint,
        {},
        {},
    )

    report = review_service.delete_document(review.document_id, default_principal())

    checkpoint = next(task for task in report.erasures if task.target == "checkpoint")
    assert checkpoint.key == f"{review.review_id}:*" and checkpoint.done_at is not None
    assert _checkpoint(saver, thread) is None
    assert _checkpoint(saver, other) is not None
    review_service.shutdown(timeout=10)


def test_a_failing_checkpointer_keeps_the_task_pending() -> None:
    from app.agent.run import erase_threads

    class Broken:
        storage: dict = {"review-x:0": {}}

        def delete_thread(self, thread_id):
            raise ConnectionError("checkpointer down")

    with pytest.raises(ConnectionError):
        erase_threads(Broken(), "review-x:*")

    store = memory_store()
    store.deletion.queue_erasures([("checkpoint", "review-x:*")])
    holder, _deleted, _forgotten = service(store)
    holder.judge = SimpleNamespace(forget=lambda key: erase_threads(Broken(), key))
    assert process_erasures(holder) == (0, 1)
    assert "checkpointer down" in store.deletion.pending_erasures()[0].last_error


def test_like_prefix_escapes_wildcards() -> None:
    from app.agent.run import _like_prefix

    assert _like_prefix("a_b%c\\:") == "a\\_b\\%c\\\\:%"


def test_retention_semantics_are_counted_from_the_last_upload_and_finished_at() -> None:
    """Stated, because they are easy to get wrong: `files` counts from the last upload,
    whatever happened to the reviews since; `reviews` from `finished_at`, which a later
    decision does not move."""
    from datetime import UTC, datetime, timedelta

    store = memory_store()
    seed(store, "one", ["a"])
    store.documents.record_upload("one", "one.pdf", "pdf", 3)
    review = store.reviews._reviews["review-one"]
    review.finished_at = datetime.now(UTC) - timedelta(days=5)
    later = datetime.now(UTC) + timedelta(seconds=1)

    assert store.deletion.expired("files", later) == ["one"]
    assert store.deletion.expired("files", datetime.now(UTC) - timedelta(days=1)) == []
    assert store.deletion.expired("reviews", datetime.now(UTC) - timedelta(days=1)) == [
        "review-one"
    ]

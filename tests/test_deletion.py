"""Deletion removes owned state, preserves shared state and drains the external outbox."""

import os
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, Document, Review, default_principal
from app.review.deletion import delete_document, process_erasures
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
    assert forgotten == ["review-one:0", "review-one:1"]
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

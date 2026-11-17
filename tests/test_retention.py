"""Retention reuses deletion and remains inert until its legal periods are enabled."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain import ClauseSpan, ClauseVerdict, Document, Review
from app.review.deletion import process_erasures
from app.review.retention import RetentionSettings, run_sweep
from app.store.memory import memory_store

NOW = datetime(2026, 11, 17, tzinfo=UTC)


def seeded_service():
    store = memory_store()
    clause = ClauseSpan(position=0, text="old", content_hash="old")
    store.documents.save(
        Document(
            document_id="old-document",
            filename="old.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash="old",
            created_at=NOW - timedelta(days=20),
        ),
        [clause],
        {"old": "old"},
    )
    store.files.put("old-document.pdf", b"pdf")
    service = SimpleNamespace(
        store=store, tracer=None, judge=SimpleNamespace(forget=lambda _: None)
    )
    service.process_erasures = lambda limit=100: process_erasures(service, limit)
    return service


def test_disabled_retention_keeps_documents_but_runs_orphan_cleanup() -> None:
    service = seeded_service()
    service.store.documents._texts["orphan"] = "orphan"

    report = run_sweep(service, RetentionSettings(enabled=False), NOW)

    assert service.store.documents.get("old-document") is not None
    assert "orphan" not in service.store.documents._texts
    assert report.expired_documents == 0
    assert report.orphans_removed["clause_texts"] == 1


def test_enabled_file_period_deletes_expired_document_through_the_outbox() -> None:
    service = seeded_service()

    report = run_sweep(
        service,
        RetentionSettings(enabled=True, files_days=10, batch=100),
        NOW,
    )

    assert report.expired_documents == 1
    assert service.store.documents.get("old-document") is None
    assert not service.store.files.exists("old-document.pdf")
    assert service.store.deletion.tombstones("old-document")[0].reason == "retention"


def test_orphan_sweep_spares_rows_younger_than_an_hour() -> None:
    service = seeded_service()
    store = service.store
    store.documents.save(
        Document(
            document_id="ingest",
            filename="new.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash="new",
        ),
        [ClauseSpan(position=0, text="young", content_hash="young")],
        {"young": "young"},
    )
    # an ingest in flight: its clause text and blob exist, its document row not yet
    store.documents._clauses.pop("ingest")
    store.documents._documents.pop("ingest")
    store.files.put("in-flight.pdf", b"pdf")
    store.verdicts.save(
        ClauseVerdict(
            content_hash="stale",
            agent_version="agent",
            verdict="valid",
            confidence=0.9,
            trace_id="stale-trace",
        )
    )
    store.verdicts._created[("stale", "agent")] = NOW - timedelta(days=400)

    removed = store.deletion.sweep_orphans()

    assert "young" in store.documents._texts and store.files.exists("in-flight.pdf")
    assert removed["verdicts"] == 1 and removed["blobs"] == 0
    assert [(t.target, t.key) for t in store.deletion.pending_erasures()] == [
        ("trace", "stale-trace")
    ]


def test_review_period_deletes_only_the_review_and_keeps_the_document() -> None:
    service = seeded_service()
    store = service.store
    store.reviews.create(
        Review(
            review_id="old-review",
            document_id="old-document",
            agent_version="agent",
            scope=["liability"],
            status="finished",
            trace_id="old-review-trace",
        )
    )
    store.reviews._reviews["old-review"].finished_at = NOW - timedelta(days=40)

    report = run_sweep(service, RetentionSettings(enabled=True, reviews_days=30), NOW)

    assert report.expired_reviews == 1
    assert store.reviews.get("old-review") is None
    assert store.documents.get("old-document") is not None
    assert store.deletion.tombstones("old-review")[0].kind == "review"
    # no tracer configured: the trace erasure is retained for the next run
    assert ("trace", "old-review-trace") in {
        (t.target, t.key) for t in store.deletion.pending_erasures()
    }


def test_trace_period_queues_old_traces_once() -> None:
    service = seeded_service()
    store = service.store
    store.reviews.create(
        Review(
            review_id="r",
            document_id="old-document",
            agent_version="agent",
            scope=["liability"],
            status="finished",
            trace_id="review-trace",
            created_at=NOW - timedelta(days=60),
        )
    )
    store.verdicts.save(
        ClauseVerdict(
            content_hash="old",
            agent_version="agent",
            verdict="valid",
            confidence=0.9,
            trace_id="verdict-trace",
        )
    )
    store.verdicts._created[("old", "agent")] = NOW - timedelta(days=60)
    settings = RetentionSettings(enabled=True, traces_days=30)

    first = run_sweep(service, settings, NOW)
    second = run_sweep(service, settings, NOW)

    assert first.expired_traces == 2 and second.expired_traces == 0
    assert store.reviews.get("r") is not None  # traces expire, the review stays


def test_tasks_past_the_attempt_limit_are_reported_and_stay_pending(caplog) -> None:
    service = seeded_service()
    service.store.deletion.queue_erasures([("trace", "t")])
    settings = RetentionSettings(erasure_max_attempts=2)

    run_sweep(service, settings, NOW)
    assert "has failed" not in caplog.text
    report = run_sweep(service, settings, NOW)

    assert report.erasures_failed == 1
    assert service.store.deletion.pending_erasures()[0].attempts == 2
    assert "has failed 2 times" in caplog.text

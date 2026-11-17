"""Retention reuses deletion and remains inert until its legal periods are enabled."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.domain import ClauseSpan, Document
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

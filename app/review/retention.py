"""Retention policy and the scheduled cleanup job.

A data class (`app.domain.DataClass`) is deleted once its period in `retention.*_days` has
passed; null keeps it until a user deletes it. Expired documents and reviews go through the
same deletion as a user's (`app.review.deletion`), so retention can never leave behind
what a user deletion would not. The job runs outside the API process: `python -m
app.review.retention` once a night (cron, a Compose service or a Kubernetes CronJob), so
two API replicas never sweep at once and a long sweep never holds a request worker.
docs/Phase 9 Design.md, "Retention", explains the periods and the choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.logging import get_logger

from app.domain import ActiveReviewError, SweepReport

if TYPE_CHECKING:
    from app.review.service import Service

log = get_logger("review.retention")

_MAX_PAGES = 1_000  # a bound, not a quota: a night's erasures fit many times over
_ALL = 1_000_000


@dataclass(frozen=True)
class RetentionSettings:
    """`retention` in configs/app.yaml. A period of None keeps that class indefinitely."""

    enabled: bool = False
    files_days: int | None = None  # documents, from their last upload
    reviews_days: int | None = None  # idle reviews, from finished_at
    traces_days: int | None = None  # Langfuse traces, from their review's or verdict's start
    erasure_max_attempts: int = 10  # then the task is reported, still pending
    batch: int = 100  # ids per data class per run


def _days(value: Any) -> int | None:
    if value is None:
        return None
    days = int(value)
    if days < 1:
        raise ValueError("retention periods are whole days, at least 1 (or null)")
    return days


def retention_settings(config: Mapping[str, Any]) -> RetentionSettings:
    block = config.get("retention") or {}
    defaults = RetentionSettings()
    return RetentionSettings(
        enabled=bool(block.get("enabled", defaults.enabled)),
        files_days=_days(block.get("files_days")),
        reviews_days=_days(block.get("reviews_days")),
        traces_days=_days(block.get("traces_days")),
        erasure_max_attempts=int(block.get("erasure_max_attempts", defaults.erasure_max_attempts)),
        batch=int(block.get("batch", defaults.batch)),
    )


def run_sweep(
    service: Service, settings: RetentionSettings, now: datetime | None = None
) -> SweepReport:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    started = datetime.now(UTC)
    expired_documents = expired_reviews = expired_traces = 0

    if settings.enabled and settings.files_days is not None:
        before = instant - timedelta(days=settings.files_days)
        for document_id in service.store.deletion.expired("files", before, settings.batch):
            try:
                service.store.deletion.delete_document(
                    document_id, deleted_by=None, reason="retention"
                )
            except (KeyError, ActiveReviewError):
                continue  # another sweep won, or a review became active after the query
            expired_documents += 1

    if settings.enabled and settings.reviews_days is not None:
        before = instant - timedelta(days=settings.reviews_days)
        for review_id in service.store.deletion.expired("reviews", before, settings.batch):
            try:
                service.store.deletion.delete_review(review_id, deleted_by=None, reason="retention")
            except (KeyError, ActiveReviewError):
                continue
            expired_reviews += 1

    if settings.enabled and settings.traces_days is not None:
        before = instant - timedelta(days=settings.traces_days)
        trace_ids = service.store.deletion.expired("traces", before, settings.batch)
        service.store.deletion.queue_erasures([("trace", trace_id) for trace_id in trace_ids])
        expired_traces = len(trace_ids)

    # Summary paragraphs written before Phase 9 carry no review id; they live as long as
    # a review would (docs/Phase 9 API.md, "Open points").
    summaries_before = None
    if settings.enabled and settings.reviews_days is not None:
        summaries_before = instant - timedelta(days=settings.reviews_days)
    orphans_removed = service.store.deletion.sweep_orphans(
        blobs=service.store.files.list_keys(), summaries_before=summaries_before
    )
    # Every due task, page by page: a failure moves its task behind the backoff, so the
    # pages advance and a run cannot loop on the same task.
    erasures_done = erasures_failed = 0
    for _page in range(_MAX_PAGES):
        done, failed = service.process_erasures(settings.batch)
        erasures_done += done
        erasures_failed += failed
        if done + failed < settings.batch:
            break
    # A task past the attempt limit stays pending (it is retried after its backoff), but
    # each run names it, so an operator learns that a trace or blob still exists.
    for task in service.store.deletion.pending_erasures(_ALL):
        if task.attempts >= settings.erasure_max_attempts:
            log.error(
                "erasure task %s (%s %s) has failed %s times: %s",
                task.task_id,
                task.target,
                task.key,
                task.attempts,
                task.last_error,
            )
    return SweepReport(
        started_at=started,
        finished_at=datetime.now(UTC),
        expired_documents=expired_documents,
        expired_reviews=expired_reviews,
        expired_traces=expired_traces,
        orphans_removed=orphans_removed,
        erasures_done=erasures_done,
        erasures_failed=erasures_failed,
    )


def main() -> None:
    """Run one sweep with production wiring; scheduling belongs to cron, not the API."""
    service, checkpoint = _production_service()
    try:
        print(service.sweep().model_dump_json())
    finally:
        service.shutdown(timeout=5)
        checkpoint.close()
        close = getattr(service.store, "close", None)
        if close is not None:
            close()


def _production_service():
    """Wire only the dependencies a standalone sweep needs, without starting FastAPI."""
    from core import langfuse_client
    from core.config import load_yaml
    from core.masking import configure as configure_masking
    from core.masking import masker_from_config
    from core.paths import CONFIGS, ROOT
    from core.settings import load_settings

    from app.agent.run import make_judge
    from app.domain import DEFAULT_OWNER_ID
    from app.review.checkpoint import postgres_checkpointer
    from app.review.limiter import build_limiter, review_settings
    from app.review.service import build_service
    from app.store.migrate import apply_pending
    from app.store.postgres import postgres_store
    from app.store.s3 import file_store_from_config

    config = load_yaml(CONFIGS / "app.yaml")
    configure_masking(masker_from_config(config))
    default_owner = str(config.get("auth", {}).get("default_owner", DEFAULT_OWNER_ID))
    apply_pending(default_owner=default_owner)
    store = postgres_store(file_store_from_config(config, ROOT))
    limiter = build_limiter(config)
    environment = load_settings()
    tracer = None
    if environment.langfuse_public_key and environment.langfuse_secret_key:
        tracer = langfuse_client.client()
    checkpoint = postgres_checkpointer()
    judge = make_judge(
        config,
        limiter=limiter,
        tracer=tracer,
        caches=store.caches,
        checkpointer=checkpoint.saver,
    )
    # Deletion (9.B) erases a review's checkpoint threads (`{review_id}:*`) on this saver
    # through `judge.forget`; without it every checkpoint erasure would stay pending.
    if not callable(getattr(judge, "forget", None)):
        raise RuntimeError("the judge cannot erase checkpoints")
    service = build_service(
        store,
        judge,
        limiter,
        tracer,
        retention_policy=retention_settings(config),
        **review_settings(config),
    )
    return service, checkpoint


if __name__ == "__main__":
    main()

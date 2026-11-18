"""FastAPI composition root with injectable tests and lazy production wiring."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from core.logging import configure as configure_logging
from core.schemas import LawChunk
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import REVIEWER_HEADER, setting
from app.api.errors import install_error_handlers
from app.api.middleware import BodyLimitMiddleware, RequestLogMiddleware
from app.api.rate_limit import IpRateLimiter
from app.api.readiness import Probe, Readiness
from app.api.routes import (
    auth,
    chat,
    decisions,
    deletion,
    documents,
    export,
    health,
    metrics,
    reviews,
)
from app.api.routes import feedback as feedback_routes
from app.api.routes import statutes as statute_routes
from app.api.routes.export import EXPORT_DRAFT_HEADER
from app.domain import DEFAULT_OWNER_ID
from app.review.interfaces import ReviewService
from app.store.interfaces import Store

# Multipart boundaries and part headers add a little to the file itself.
BODY_SLACK_BYTES = 256 * 1024


def create_app(
    *,
    store: Store,
    service: ReviewService,
    statutes: Mapping[str, LawChunk],
    feedback: Any,
    settings: Any,
    readiness: Mapping[str, Probe] | None = None,
    on_shutdown: Iterable[Callable[[], None]] = (),
) -> FastAPI:
    """Build an application whose external dependencies are all supplied by its caller.

    `on_shutdown` closes what the caller opened beyond the store (the checkpoint pool),
    after the service has stopped using it.
    """
    closers = list(on_shutdown)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        service.resume_running()
        try:
            yield
        finally:
            service.shutdown(timeout=10)
            close_store = getattr(store, "close", None)
            if close_store is not None:
                close_store()
            for close in closers:
                close()

    application = FastAPI(title="AGB-Check API", version="1", lifespan=lifespan)
    application.state.store = store
    application.state.service = service
    application.state.statutes = dict(statutes)
    application.state.feedback = feedback
    application.state.settings = settings
    application.state.limiter = getattr(service, "limiter", None)
    per_minute = int(setting(settings, "api.rate_limit_per_minute", 60))
    capacity = int(setting(settings, "api.rate_limit_burst", per_minute))
    application.state.rate_limiter = IpRateLimiter(capacity=capacity, per_minute=per_minute)
    application.state.readiness = Readiness(readiness or {})

    # Only the frontend's origin may call the API from a browser; an empty list refuses all.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(setting(settings, "api.cors_origins", [])),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID", REVIEWER_HEADER],
        expose_headers=["X-Request-Id", "Content-Disposition", EXPORT_DRAFT_HEADER],
        max_age=600,
    )
    # Body size is checked before any parser runs; the slack covers multipart framing.
    upload_bytes = int(setting(settings, "ingest.max_upload_mb", 20)) * 1024 * 1024
    application.add_middleware(BodyLimitMiddleware, max_bytes=upload_bytes + BODY_SLACK_BYTES)
    # Added last, so it is the outermost layer and logs every request, including preflights.
    application.add_middleware(RequestLogMiddleware)

    install_error_handlers(application)
    for route in (
        documents,
        reviews,
        feedback_routes,
        decisions,
        export,
        chat,
        deletion,
        auth,
        metrics,
        statute_routes,
        health,
    ):
        application.include_router(route.router, prefix="/api/v1")
    return application


def load_statutes(path: Path) -> dict[str, LawChunk]:
    """Validate the statute JSONL once so request handling is a dictionary lookup."""
    chunks: dict[str, LawChunk] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                chunk = LawChunk.model_validate(json.loads(line))
                chunks[chunk.chunk_id] = chunk
    return chunks


def build_app() -> FastAPI:
    """Wire production adapters lazily for ``uvicorn app.api.main:app --factory``."""
    from functools import partial

    from core import langfuse_client
    from core.config import load_yaml
    from core.masking import configure as configure_masking
    from core.masking import masker_from_config
    from core.paths import CONFIGS, PROCESSED, ROOT
    from core.routing import classify_topics
    from core.settings import load_settings

    from app.agent.run import make_judge
    from app.api.readiness import production_probes
    from app.review.chat import make_chat_model
    from app.review.checkpoint import postgres_checkpointer
    from app.review.corrections import Corrections, CorrectionSettings
    from app.review.deliverable import deliverable_settings
    from app.review.feedback import FeedbackService
    from app.review.limiter import build_limiter, review_settings
    from app.review.retention import retention_settings
    from app.review.service import build_service
    from app.review.summary import make_summary_model
    from app.store.migrate import apply_pending
    from app.store.postgres import postgres_store
    from app.store.s3 import file_store_from_config

    config = load_yaml(CONFIGS / "app.yaml")
    configure_logging(json_logs=True)
    environment = load_settings()
    # Phase 9 (9.A): the masking boundary every model and trace payload passes through;
    # None (masking off) until `privacy.masking.enabled`. The masker loads now, not on the
    # first clause: a missing spaCy model or Presidio stops the start (MaskerUnavailable)
    # unless `privacy.masking.allow_regex_only` accepts the regex layer alone.
    masker = masker_from_config(config)
    configure_masking(masker)
    load_masker = getattr(masker, "load", None)
    if callable(load_masker):
        load_masker()
    apply_pending(default_owner=setting(config, "auth.default_owner", DEFAULT_OWNER_ID))
    store = postgres_store(file_store_from_config(config, ROOT))
    statutes = load_statutes(PROCESSED / "bgb_chunks.jsonl")
    limiter = build_limiter(config)
    tracer = None
    if environment.langfuse_public_key and environment.langfuse_secret_key:
        tracer = langfuse_client.client()
    # Interrupted (uncertain) clauses wait in Postgres, so a decision resumes them after a
    # restart (Phase 7, 7.B). Tables are created or migrated here, once per process.
    checkpointer = postgres_checkpointer()
    judge = make_judge(
        config,
        limiter=limiter,
        tracer=tracer,
        caches=store.caches,
        checkpointer=checkpointer.saver,
    )
    # Deletion (9.B) erases a review's checkpoint threads (`{review_id}:*`) on this saver
    # through `judge.forget`; without it every checkpoint erasure would stay pending.
    if not callable(getattr(judge, "forget", None)):
        raise RuntimeError("the judge cannot erase checkpoints")
    feedback = FeedbackService(store, tracer)
    corrections = Corrections(tracer, CorrectionSettings.from_config(config))
    router_model = setting(config, "models.router.name", "jina-embeddings-v3")
    service = build_service(
        store,
        judge,
        limiter,
        tracer,
        statutes=statutes,
        router=partial(classify_topics, model=router_model),
        router_model=router_model,
        feedback=feedback,
        deliverable=deliverable_settings(config),
        summary_model=make_summary_model(config, limiter=limiter),
        chat_model=make_chat_model(config, limiter=limiter),
        retention_policy=retention_settings(config),
        **review_settings(config),
    )
    service.corrections = corrections
    return create_app(
        store=store,
        service=service,
        statutes=statutes,
        feedback=feedback,
        settings=config,
        readiness=production_probes(
            alias=setting(config, "store.alias", "bgb_agb"),
            router_model=router_model,
            explain_model=setting(config, "models.explain.name", "gemini-3.8-flash"),
        ),
        on_shutdown=[checkpointer.close],
    )


app = build_app

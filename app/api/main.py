"""FastAPI composition root with injectable tests and lazy production wiring."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from core.schemas import LawChunk
from fastapi import FastAPI

from app.api.deps import setting
from app.api.errors import install_error_handlers
from app.api.rate_limit import IpRateLimiter
from app.api.routes import decisions, documents, health, reviews
from app.api.routes import feedback as feedback_routes
from app.api.routes import statutes as statute_routes
from app.review.interfaces import ReviewService
from app.store.interfaces import Store


def create_app(
    *,
    store: Store,
    service: ReviewService,
    statutes: Mapping[str, LawChunk],
    feedback: Any,
    settings: Any,
) -> FastAPI:
    """Build an application whose external dependencies are all supplied by its caller."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        service.resume_running()
        try:
            yield
        finally:
            shutdown = getattr(service, "shutdown", None)
            if callable(shutdown):
                result = shutdown()
                if inspect.isawaitable(result):
                    await result

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

    install_error_handlers(application)
    for route in (documents, reviews, feedback_routes, decisions, statute_routes, health):
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
    from core import langfuse_client
    from core.budget import SharedLimiter
    from core.config import load_yaml
    from core.paths import CONFIGS, PROCESSED
    from core.settings import load_settings

    from app.agent.run import make_judge
    from app.review.feedback import FeedbackService

    # The runner branch is merged after this one; keeping this import here lets
    # route tests run against their fake without requiring its implementation.
    from app.review.service import build_service
    from app.store.files import LocalFileStore
    from app.store.migrate import apply_pending
    from app.store.postgres import postgres_store

    config = load_yaml(CONFIGS / "app.yaml")
    environment = load_settings()
    apply_pending()
    store = postgres_store(LocalFileStore())
    limiter = SharedLimiter(
        rpm=setting(config, "limits.rpm", None),
        daily_budget=setting(config, "limits.daily_requests", None),
    )
    tracer = None
    if environment.langfuse_public_key and environment.langfuse_secret_key:
        with suppress(Exception):
            tracer = langfuse_client.client()
    judge = make_judge(config, limiter=limiter, tracer=tracer, caches=store.caches)
    service = build_service(store=store, judge=judge, limiter=limiter, tracer=tracer)
    return create_app(
        store=store,
        service=service,
        statutes=load_statutes(PROCESSED / "bgb_chunks.jsonl"),
        feedback=FeedbackService(store, tracer),
        settings=config,
    )


app = build_app

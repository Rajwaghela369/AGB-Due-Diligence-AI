"""A fully offline API for the Playwright end-to-end test; never used in production.

It is the real application (real routes, ingest, review service, SSE and feedback
service) with every external dependency replaced: an in-memory store, a temp-dir
file store, a keyword judge instead of Gemini, no Langfuse tracer, a Langfuse fake that
records scores, and no Gotenberg (so PDF uploads only). Two extra routes under
``/api/v1/__test__`` expose what the test must assert on from outside the process: how many
times the judge ran, and which scores feedback wrote. They exist only on this app.

    uvicorn app.api.testing:build_test_app --factory --port 8010
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from core.schemas import LawChunk
from fastapi import APIRouter, FastAPI, Request

from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, StepObservations
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.files import LocalFileStore
from app.store.memory import memory_store

# Slow enough that a browser can watch clause states change, fast enough for CI.
DEFAULT_JUDGE_DELAY_SECONDS = 0.15

_STATUTE = LawChunk(
    chunk_id="bgb-309-7",
    law="BGB",
    paragraph="§ 309",
    item="7",
    title="Haftungsausschluss bei Verletzung von Leben, Körper, Gesundheit",
    citation="§ 309 Nr. 7 BGB",
    topics=["liability"],
    embed_text="Haftungsausschluss",
    text="Unwirksam ist ein Ausschluss oder eine Begrenzung der Haftung.",
    source_dump="test",
)


class KeywordJudge:
    """Deterministic verdicts: „ausgeschlossen“ is void, „Gewährleistung“ uncertain, else valid."""

    agent_version = "agent-e2e"

    def __init__(self, delay: float = DEFAULT_JUDGE_DELAY_SECONDS) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, clause: ClauseSpan, *, parent: Any = None) -> ClauseVerdict:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if "ausgeschlossen" in clause.text:
            verdict, confidence, basis = "potentially_void", 0.92, [_STATUTE.chunk_id]
        elif "Gewährleistung" in clause.text:
            verdict, confidence, basis = "uncertain", 0.55, []
        else:
            verdict, confidence, basis = "valid", 0.9, []
        return ClauseVerdict(
            content_hash=clause.content_hash,
            agent_version=self.agent_version,
            verdict=verdict,
            legal_basis=basis,
            candidates=basis,
            reason_de="Testbegründung zur Klausel.",
            confidence=confidence,
            trace_id="trace-e2e",
            observations=StepObservations(
                clause_span=f"span-{clause.content_hash[:8]}",
                judge=f"judge-{clause.content_hash[:8]}",
                retrieval=f"retrieval-{clause.content_hash[:8]}",
                explanation=f"explanation-{clause.content_hash[:8]}",
            ),
        )


class RecordingLangfuse:
    """Just enough of the Langfuse client for `FeedbackService`: it keeps every score."""

    def __init__(self) -> None:
        self.scores: dict[str, dict[str, Any]] = {}
        self.api = _ScoresApi(self.scores)

    def create_score(self, **kwargs: Any) -> None:
        self.scores[kwargs["score_id"]] = kwargs


class _ScoresApi:
    def __init__(self, scores: dict[str, dict[str, Any]]) -> None:
        self.scores = _Scores(scores)


class _Scores:
    def __init__(self, scores: dict[str, dict[str, Any]]) -> None:
        self._scores = scores

    def delete(self, score_id: str) -> None:
        self._scores.pop(score_id, None)


def build_test_app() -> FastAPI:
    delay = float(os.environ.get("AGB_TEST_JUDGE_DELAY", DEFAULT_JUDGE_DELAY_SECONDS))
    root = Path(tempfile.mkdtemp(prefix="agb-e2e-"))
    store = memory_store()
    store.files = LocalFileStore(root)  # type: ignore[misc]  # real files, so the PDF is served
    judge = KeywordJudge(delay)
    langfuse = RecordingLangfuse()
    feedback = FeedbackService(store, langfuse)
    service = build_service(
        store,
        judge,
        build_limiter({}),
        tracer=None,
        max_concurrency=2,
        statutes={_STATUTE.chunk_id: _STATUTE},
        feedback=feedback,
    )
    application = create_app(
        store=store,
        service=service,
        statutes={_STATUTE.chunk_id: _STATUTE},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}, "api": {"rate_limit_per_minute": 100000}},
    )

    router = APIRouter(prefix="/__test__")

    @router.get("/judge-calls")
    def judge_calls(_request: Request) -> dict[str, int]:
        return {"calls": judge.calls}

    @router.get("/scores")
    def scores(_request: Request) -> dict[str, list[dict[str, Any]]]:
        return {"scores": list(langfuse.scores.values())}

    application.include_router(router, prefix="/api/v1")
    return application

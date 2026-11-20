"""Operations surface of the API: CORS, request ids, JSON log fields and readiness.

Everything runs against the memory store and fakes. Logs are captured by pointing the
module's own handler at a buffer, so the assertions read the exact lines an operator
would see in production.
"""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest
from core.logging import configure
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.readiness import production_probes, run_probes
from app.domain import ClauseVerdict, StepObservations
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.memory import memory_store
from tests.test_api import FakeReviewService, _save_document

ALLOWED = "http://localhost:5173"
SETTINGS = {
    "ingest": {"max_upload_mb": 20},
    "api": {"cors_origins": [ALLOWED], "rate_limit_per_minute": 600, "rate_limit_burst": 600},
}


def _app(max_upload_mb: int = 20, **kwargs):
    store = memory_store()
    settings = {**SETTINGS, "ingest": {"max_upload_mb": max_upload_mb}}
    return create_app(
        store=store,
        service=FakeReviewService(store),
        statutes={},
        feedback=FeedbackService(store, None),
        settings=kwargs.pop("settings", settings),
        **kwargs,
    ), store


@pytest.fixture
def logs():
    """Route this process's log handler into a buffer, as JSON, for the test's duration."""
    buffer = io.StringIO()
    configure(json_logs=True, stream=buffer)
    yield buffer
    configure(json_logs=False)


def _json_lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# --- CORS -----------------------------------------------------------------------------


def test_cors_allows_only_the_configured_origin():
    application, _store = _app()
    with TestClient(application) as client:
        allowed = client.get("/api/v1/health", headers={"Origin": ALLOWED})
        denied = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        preflight = client.options(
            "/api/v1/reviews/review-1/events",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "last-event-id",
            },
        )
        denied_preflight = client.options(
            "/api/v1/reviews/review-1/events",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert allowed.headers["access-control-allow-origin"] == ALLOWED
    assert "access-control-allow-origin" not in denied.headers
    assert preflight.status_code == 200
    assert "last-event-id" in preflight.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-origin" not in denied_preflight.headers


def test_cors_exposes_the_request_id_header():
    application, _store = _app()
    with TestClient(application) as client:
        response = client.get("/api/v1/health", headers={"Origin": ALLOWED})
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


def test_no_origins_configured_refuses_every_browser_origin():
    application, _store = _app(settings={"api": {"cors_origins": []}})
    with TestClient(application) as client:
        response = client.get("/api/v1/health", headers={"Origin": ALLOWED})
    assert "access-control-allow-origin" not in response.headers


# --- request ids and access log -----------------------------------------------------


def test_every_response_gets_a_request_id_and_a_supplied_one_is_kept():
    application, _store = _app()
    with TestClient(application) as client:
        generated = client.get("/api/v1/health")
        kept = client.get("/api/v1/health", headers={"X-Request-Id": "edge-42"})
        replaced = client.get("/api/v1/health", headers={"X-Request-Id": "bad id with spaces"})

    assert len(generated.headers["x-request-id"]) == 32
    assert kept.headers["x-request-id"] == "edge-42"
    assert replaced.headers["x-request-id"] != "bad id with spaces"


def test_access_log_line_is_one_json_object_with_request_fields(logs):
    application, _store = _app()
    with TestClient(application) as client:
        response = client.get(
            "/api/v1/statutes/missing?token=secret", headers={"X-Request-Id": "req-7"}
        )

    lines = [line for line in _json_lines(logs) if line["logger"] == "api.request"]
    assert len(lines) == 1
    line = lines[0]
    assert line["request_id"] == "req-7"
    assert (line["method"], line["path"], line["status"]) == (
        "GET",
        "/api/v1/statutes/missing",
        404,
    )
    assert isinstance(line["duration_ms"], float)
    assert line["level"] == "INFO"
    assert response.headers["x-request-id"] == "req-7"
    # The query string can carry credentials, so it never reaches the log.
    assert not any("secret" in json.dumps(line) for line in lines)


def test_review_log_lines_carry_review_document_and_trace_ids(logs):
    from core.logging import get_logger

    class FakeSpan:
        trace_id = "trace-ops-1"

        def update(self, **kwargs):
            pass

        def end(self):
            pass

    class FakeTracer:
        def start_observation(self, **kwargs):
            return FakeSpan()

    class Judge:
        agent_version = "agent-ops"

        def __call__(self, clause, *, parent=None) -> ClauseVerdict:
            # Code deep in the judge logs with no context of its own; the runner's bind() covers it.
            log.info("judging clause")
            return ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version=self.agent_version,
                verdict="valid",
                confidence=0.9,
                trace_id="trace-ops-1",
                observations=StepObservations(
                    clause_span="s", judge="j", retrieval="r", explanation="e"
                ),
            )

    store = memory_store()
    log = get_logger("tests.judge")
    service = build_service(store, Judge(), build_limiter({}), FakeTracer())
    _save_document(store)
    review = service.start("doc-1", ["liability"])
    for _event in service.events(review.review_id, follow=True):
        pass

    lines = _json_lines(logs)
    judged = [line for line in lines if line["logger"] == "tests.judge"]
    assert judged, "the judge logged nothing"
    for line in judged:
        assert line["review_id"] == review.review_id
        assert line["document_id"] == "doc-1"
        assert line["trace_id"] == "trace-ops-1"


def test_json_formatter_reads_context_at_emission_time(logs):
    from core.logging import bind, get_logger

    log = get_logger("tests.ops")
    with bind(review_id="r-1", trace_id="t-1"):
        log.info("inside", extra={"status": 200})
    log.info("outside")

    inside, outside = [line for line in _json_lines(logs) if line["logger"] == "tests.ops"]
    assert (inside["review_id"], inside["trace_id"], inside["status"]) == ("r-1", "t-1", 200)
    assert "review_id" not in outside


# --- readiness ------------------------------------------------------------------------


def test_ready_is_200_when_every_probe_passes():
    application, _store = _app(readiness={"neon": lambda: True, "qdrant": lambda: True})
    with TestClient(application) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 200
    assert response.json() == {"ready": True, "checks": {"neon": True, "qdrant": True}}


def test_ready_is_503_and_names_the_failing_check():
    def broken() -> bool:
        raise ConnectionError("no route")

    application, _store = _app(
        readiness={"neon": lambda: True, "gemini": lambda: False, "jina": broken}
    )
    with TestClient(application) as client:
        response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json() == {
        "ready": False,
        "checks": {"neon": True, "gemini": False, "jina": False},
    }


def test_slow_probe_fails_at_its_deadline_and_others_still_report():
    started = time.perf_counter()
    results = run_probes(
        {"fast": lambda: True, "hung": lambda: time.sleep(5) or True},
        timeout=0.2,
    )
    elapsed = time.perf_counter() - started
    assert results == {"fast": True, "hung": False}
    assert elapsed < 2


def test_probes_run_in_parallel():
    started = time.perf_counter()
    results = run_probes({f"p{index}": lambda: time.sleep(0.3) or True for index in range(4)})
    elapsed = time.perf_counter() - started
    assert all(results.values())
    assert elapsed < 0.9  # four 0.3 s probes take about 0.3 s together, 1.2 s in sequence


def test_production_probes_name_the_four_dependencies_without_calling_them(monkeypatch):
    # Blank the keys explicitly: a developer's .env must never turn this into a live call.
    for name in ("GEMINI_API_KEY", "JINA_API_KEY"):
        monkeypatch.setenv(name, "")
    probes = production_probes(
        alias="bgb_agb", router_model="jina-embeddings-v3", explain_model="gemini-3.8-flash"
    )
    assert set(probes) == {"neon", "qdrant", "gemini", "jina"}
    # Without credentials the two key-based probes report failure rather than raising.
    assert run_probes({"gemini": probes["gemini"], "jina": probes["jina"]}) == {
        "gemini": False,
        "jina": False,
    }


# --- readiness bounds -----------------------------------------------------------------


def test_readiness_result_is_cached_for_thirty_seconds():
    from app.api.readiness import Readiness

    calls = {"n": 0}
    now = {"t": 1000.0}

    def probe() -> bool:
        calls["n"] += 1
        return True

    checker = Readiness({"jina": probe}, clock=lambda: now["t"])
    assert checker.check() == {"jina": True}
    assert checker.check() == {"jina": True}
    assert calls["n"] == 1  # a burst of /ready calls does not re-bill the probe

    now["t"] += 31
    checker.check()
    assert calls["n"] == 2


def test_hung_probe_does_not_grow_threads_under_concurrent_readiness_calls():
    import threading

    from app.api.readiness import MAX_WORKERS, Readiness

    release = threading.Event()
    calls = {"n": 0}

    def hung() -> bool:
        calls["n"] += 1
        release.wait(timeout=10)
        return True

    checker = Readiness({"neon": hung}, timeout=0.05, ttl=0)  # ttl 0: every call probes
    baseline = threading.active_count()
    try:
        threads = [threading.Thread(target=checker.check) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        for _ in range(10):
            assert checker.check() == {"neon": False}
        grown = threading.active_count() - baseline
    finally:
        release.set()

    assert calls["n"] == 1  # the hung probe is not started again while it is still running
    assert grown <= MAX_WORKERS


def test_production_clients_are_created_with_explicit_timeouts(monkeypatch):
    import psycopg
    import qdrant_client
    import requests
    from core import settings as settings_module
    from google import genai

    seen: dict[str, dict] = {}
    fake_settings = SimpleNamespace(
        database_url="postgresql://x",
        qdrant_url="https://q.example",
        qdrant_api_key="k",
        gemini_api_key="g",
        jina_api_key="j",
    )
    monkeypatch.setattr(settings_module, "load_settings", lambda: fake_settings)

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql):
            seen.setdefault("neon_sql", []).append(sql)
            return SimpleNamespace(fetchone=lambda: (1,))

    monkeypatch.setattr(
        psycopg, "connect", lambda url, **kw: (seen.setdefault("neon", kw), FakeConn())[1]
    )

    class FakeQdrant:
        def __init__(self, **kw):
            seen["qdrant"] = kw

        def get_aliases(self):
            return SimpleNamespace(aliases=[SimpleNamespace(alias_name="bgb_agb")])

        def close(self):
            pass

    monkeypatch.setattr(qdrant_client, "QdrantClient", FakeQdrant)

    class FakeModels:
        def get(self, model):
            seen["gemini_model"] = model

    class FakeGenai:
        def __init__(self, **kw):
            seen["gemini"] = kw
            self.models = FakeModels()

    monkeypatch.setattr(genai, "Client", FakeGenai)

    def fake_post(url, **kw):
        seen["jina"] = kw
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(requests, "post", fake_post)

    probes = production_probes(
        alias="bgb_agb", router_model="jina-embeddings-v3", explain_model="gemini-3.8-flash"
    )
    assert run_probes(probes) == {"neon": True, "qdrant": True, "gemini": True, "jina": True}

    assert seen["neon"]["connect_timeout"] >= 1
    assert "options" not in seen["neon"]  # Neon's pooler rejects startup options
    assert any("statement_timeout" in sql for sql in seen["neon_sql"])
    assert seen["qdrant"]["timeout"] >= 1
    assert seen["gemini"]["http_options"]["timeout"] > 0
    assert seen["gemini_model"] == "gemini-3.8-flash"
    assert seen["jina"]["timeout"] > 0


# --- unhandled errors and request ids -------------------------------------------------


def test_unhandled_error_is_a_500_body_that_still_carries_the_request_id():
    application, _store = _app()

    def boom():
        # RuntimeError is deliberately mapped to 503 by the service error handlers; use another.
        raise ZeroDivisionError("internal detail that must not leak")

    application.add_api_route("/api/v1/boom", boom)
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/boom", headers={"X-Request-Id": "trace-me-500"})

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "trace-me-500"
    assert response.json() == {"code": "internal_error", "message": "Unerwarteter Serverfehler."}
    assert "internal detail" not in response.text


# --- streamed body limit --------------------------------------------------------------


def _chunks(total: int, step: int = 64 * 1024):
    sent = 0
    while sent < total:
        size = min(step, total - sent)
        sent += size
        yield b"x" * size


def _chunked_multipart(total: int, boundary: str = "BOUNDARY"):
    """A multipart body streamed in chunks, so httpx sends it with no Content-Length."""
    yield (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode()
    yield from _chunks(total)
    yield f"\r\n--{boundary}--\r\n".encode()


def test_chunked_oversized_multipart_is_413_and_the_parser_never_finishes(monkeypatch):
    application, _store = _app(max_upload_mb=1)
    monkeypatch.setattr(
        "app.api.routes.documents.ingest_file",
        lambda *a, **k: pytest.fail("the upload must not be parsed past the limit"),
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/documents",
            content=_chunked_multipart(3 * 1024 * 1024),
            headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "too_large"


def test_chunked_oversized_json_is_413(monkeypatch):
    application, _store = _app(max_upload_mb=1)
    monkeypatch.setattr(
        "app.api.routes.documents.ingest_text",
        lambda *a, **k: pytest.fail("pasted text must not be parsed past the limit"),
    )

    def body():
        yield b'{"text": "'
        yield from _chunks(2 * 1024 * 1024)
        yield b'"}'

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/documents",
            content=body(),
            headers={"content-type": "application/json"},
        )

    assert (response.status_code, response.json()["code"]) == (413, "too_large")


def test_oversized_declared_length_is_refused_before_the_app_runs(monkeypatch):
    application, _store = _app(max_upload_mb=1)
    monkeypatch.setattr(
        "app.api.routes.documents.ingest_file",
        lambda *a, **k: pytest.fail("an oversized declared length must not reach the app"),
    )
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/documents",
            content=b"x" * (2 * 1024 * 1024),
            headers={"content-type": "application/pdf"},
        )

    assert (response.status_code, response.json()["code"]) == (413, "too_large")


def test_chunked_body_under_the_limit_still_reaches_the_app():
    application, _store = _app(max_upload_mb=1)
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/documents",
            content=_chunked_multipart(256 * 1024),
            headers={"content-type": "multipart/form-data; boundary=BOUNDARY"},
        )
    # Reaches ingest, which rejects the non-document bytes: the limit did not intercept it.
    assert (response.status_code, response.json()["code"]) == (415, "unsupported_type")

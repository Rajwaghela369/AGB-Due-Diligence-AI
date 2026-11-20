"""Readiness probes for the four services the API cannot answer without.

Three bounds keep `/ready` from becoming a thread leak or a billing loop:

- Every client gets its own deadline (connect, statement and read timeouts), so a probe
  that is blackholed ends by itself rather than running forever.
- All probes share one bounded pool, and a probe that is still running from an earlier
  call is not started again. A dependency that hangs costs at most one thread per probe.
- The combined result is cached for `CACHE_SECONDS`, so polling `/ready` does not
  repeatedly bill the embedding API or the Gemini quota.

The probes are passed in by the caller, so tests use fakes and never touch the network.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait

PROBE_TIMEOUT_SECONDS = 2.0
CACHE_SECONDS = 30.0
MAX_WORKERS = 8

Probe = Callable[[], bool]

# Module-level on purpose: one pool for the process, so repeated calls reuse its threads.
_POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ready")
_pool_lock = threading.Lock()
_running: dict[Probe, Future] = {}


def run_probes(
    probes: Mapping[str, Probe], *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, bool]:
    if not probes:
        return {}
    futures: dict[str, Future] = {}
    with _pool_lock:
        for finished in [probe for probe, future in _running.items() if future.done()]:
            del _running[finished]
        for name, probe in probes.items():
            future = _running.get(probe)
            if future is None:
                future = _POOL.submit(probe)
                _running[probe] = future
            # A probe still hung from an earlier call is reported as failed, not started again.
            futures[name] = future
    wait(futures.values(), timeout=timeout)
    results: dict[str, bool] = {}
    for name, future in futures.items():
        if not future.done():
            results[name] = False
            continue
        try:
            results[name] = bool(future.result())
        except Exception:  # noqa: BLE001 - a failing dependency reports, it does not raise
            results[name] = False
    return results


class Readiness:
    """`run_probes` behind a short cache, so a burst of `/ready` calls probes once."""

    def __init__(
        self,
        probes: Mapping[str, Probe],
        *,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        ttl: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.probes = dict(probes)
        self.timeout = timeout
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: dict[str, bool] | None = None
        self._cached_at = 0.0

    def check(self) -> dict[str, bool]:
        # Held across the probing, so concurrent callers share one round instead of queuing many.
        with self._lock:
            now = self.clock()
            if self._cached is None or now - self._cached_at >= self.ttl:
                self._cached = run_probes(self.probes, timeout=self.timeout)
                self._cached_at = now
            return dict(self._cached)


def production_probes(*, alias: str, router_model: str, explain_model: str) -> dict[str, Probe]:
    """The real checks behind `/ready`. Imports are local so the app loads without credentials."""
    timeout_ms = int(PROBE_TIMEOUT_SECONDS * 1000)

    def neon() -> bool:
        import psycopg
        from core.settings import load_settings

        url = load_settings().database_url
        if not url:
            return False
        # connect_timeout bounds the handshake; statement_timeout bounds the query itself.
        # Set after connecting: Neon's pooler rejects the startup `options` parameter.
        with psycopg.connect(url, connect_timeout=int(PROBE_TIMEOUT_SECONDS)) as conn:
            conn.execute(f"set statement_timeout = {int(timeout_ms)}")
            return conn.execute("select 1").fetchone() == (1,)

    def qdrant() -> bool:
        from core.settings import load_settings
        from qdrant_client import QdrantClient

        settings = load_settings()
        if not settings.qdrant_url:
            return False
        client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=int(PROBE_TIMEOUT_SECONDS),
        )
        try:
            return any(item.alias_name == alias for item in client.get_aliases().aliases)
        finally:
            client.close()

    def gemini() -> bool:
        from core.settings import load_settings
        from google import genai

        api_key = load_settings().gemini_api_key
        if not api_key:
            return False
        # A metadata read of the explain model on the default API version. The judge model is
        # served only on v1beta, so it is not probed here. Neither a decision nor a generation
        # call belongs in readiness.
        client = genai.Client(api_key=api_key, http_options={"timeout": timeout_ms})
        client.models.get(model=explain_model)
        return True

    def jina() -> bool:
        import requests
        from core.embeddings import ENDPOINT, TASK_QUERY
        from core.settings import load_settings

        api_key = load_settings().jina_api_key
        if not api_key:
            return False
        response = requests.post(
            ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": router_model, "task": TASK_QUERY, "input": ["ok"]},
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        return response.status_code == 200

    return {"neon": neon, "qdrant": qdrant, "gemini": gemini, "jina": jina}

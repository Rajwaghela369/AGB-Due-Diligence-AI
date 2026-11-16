"""Readiness probes for the four services the API cannot answer without.

Each probe runs in its own thread with a short deadline, all at once, so one slow
dependency cannot delay the others or hold the response past the deadline. A probe that
raises, returns false or misses the deadline counts as failed. The probes are passed in
by the caller, so tests use fakes and never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, wait

PROBE_TIMEOUT_SECONDS = 2.0

Probe = Callable[[], bool]


def run_probes(
    probes: Mapping[str, Probe], *, timeout: float = PROBE_TIMEOUT_SECONDS
) -> dict[str, bool]:
    if not probes:
        return {}
    pool = ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="ready")
    try:
        futures = {name: pool.submit(probe) for name, probe in probes.items()}
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
    finally:
        # A probe still waiting on the network keeps its thread; the response does not wait for it.
        pool.shutdown(wait=False, cancel_futures=True)


def production_probes(*, alias: str, router_model: str, explain_model: str) -> dict[str, Probe]:
    """The real checks behind `/ready`. Imports are local so the app loads without credentials."""

    def neon() -> bool:
        from core.db import connection

        with connection() as conn:
            return conn.execute("select 1").fetchone() == (1,)

    def qdrant() -> bool:
        from core.qdrant import client

        return any(item.alias_name == alias for item in client().get_aliases().aliases)

    def gemini() -> bool:
        from core.settings import load_settings
        from google import genai

        api_key = load_settings().gemini_api_key
        if not api_key:
            return False
        # A metadata read of the explain model on the default API version. The judge model is
        # served only on v1beta, so it is not probed here. Neither a decision nor a generation
        # call belongs in readiness.
        genai.Client(api_key=api_key).models.get(model=explain_model)
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

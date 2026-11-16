"""Phase 5 seams that must remain offline: shared limits, caches and tracing."""

from types import SimpleNamespace

import pytest
from core.budget import BudgetExceeded, SharedLimiter
from core.embeddings import embed_texts
from core.retrieval import Retrieved

from app.agent import prompts, run, task
from app.domain import ClauseSpan


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Cache:
    def __init__(self) -> None:
        self.embeddings: dict[tuple[str, str], list[float]] = {}

    def get_embedding(self, digest, model):
        return self.embeddings.get((digest, model))

    def put_embedding(self, digest, model, vector):
        self.embeddings[(digest, model)] = vector


def test_shared_limiter_paces_and_rolls_its_daily_budget():
    clock = Clock()
    limiter = SharedLimiter(rpm=2, daily_budget=2, clock=lambda: clock.now, sleep=clock.sleep)

    limiter.acquire(2)
    assert limiter.remaining_today() == 0
    limiter.acquire()

    assert limiter.waiting_reason() is None
    assert clock.sleeps[0] > 60 * 60, "daily limit waits until UTC midnight, not one minute"
    assert limiter.remaining_today() == 1


def test_shared_limiter_timeout_preserves_the_daily_cap():
    clock = Clock()
    limiter = SharedLimiter(daily_budget=1, clock=lambda: clock.now, sleep=clock.sleep)
    limiter.acquire()

    with pytest.raises(BudgetExceeded, match="daily_budget"):
        limiter.acquire(timeout=1)
    assert limiter.remaining_today() == 0


def test_embedding_cache_hit_avoids_provider_configuration():
    cache = Cache()
    cache.put_embedding(
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", "model", [1.0, 2.0]
    )

    result = embed_texts(["hello"], "model", 2, "retrieval.query", cache=cache)

    assert result.vectors == [[1.0, 2.0]]
    assert result.tokens == 0


class TraceObservation:
    def __init__(self, trace: "Trace", name: str, parent: str | None) -> None:
        self.trace = trace
        self.name = name
        self.parent = parent
        self.id = f"obs-{len(trace.observations) + 1}"

    def __enter__(self):
        self.trace.observations.append(self)
        return self

    def __exit__(self, *args):
        return None

    def update(self, **kwargs):
        self.output = kwargs

    def start_as_current_observation(self, *, name, **kwargs):
        return TraceObservation(self.trace, name, self.id)

    def get_current_trace_id(self):
        return "trace-1"


class Trace:
    def __init__(self) -> None:
        self.observations: list[TraceObservation] = []

    def start_as_current_observation(self, *, name, **kwargs):
        return TraceObservation(self, name, None)


def _config() -> dict:
    return {
        "models": {"judge": {"provider": "gemini", "name": "judge"}},
        "prompts": {"label": "evaluation"},
        "retrieval": {"top_k": 1, "search_depth": 1},
        "agent": {"explain_enabled": True},
    }


def _clause() -> ClauseSpan:
    return ClauseSpan(
        position=0, number="1", heading="Haftung", text="Wir haften nie.", content_hash="h"
    )


def test_make_judge_nests_steps_and_keeps_a_failed_explanation(monkeypatch):
    prompts.clear_cache()
    monkeypatch.setattr(task, "index_hash", lambda: "index")
    tracer = Trace()
    chunk = Retrieved("bgb-309-7-a", "§ 309", "text", 1.0, 1)
    # The provider-shaped fake keeps this test fully offline.
    judge_client = SimpleNamespace(
        decisions=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                answers={
                    "is_void": SimpleNamespace(noul=0.9, confidence=0.9, probabilities=None),
                    "bgb-309-7-a": SimpleNamespace(noul=0.9, confidence=0.9, probabilities=None),
                },
                usage=SimpleNamespace(total_input_tokens=2, total_output_tokens=0),
            )
        ),
        models=SimpleNamespace(
            generate_content=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("explain down"))
        ),
    )
    judge = run.make_judge(
        _config(),
        limiter=SharedLimiter(),
        client=judge_client,
        tracer=tracer,
        router=lambda text: "",
        retriever=lambda *args, **kwargs: [chunk],
    )
    parent = TraceObservation(tracer, "review", None)
    with parent:
        verdict = judge(_clause(), parent=parent)

    assert verdict.reason_unavailable is True
    assert verdict.observations.clause_span
    assert verdict.observations.judge
    assert verdict.observations.retrieval
    assert verdict.observations.explanation
    children = [
        item for item in tracer.observations if item.parent == verdict.observations.clause_span
    ]
    assert {item.name for item in children} >= {"retrieve", "judge", "explain"}

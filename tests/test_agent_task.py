"""The provider wiring: config reads, run metadata, and the output contract."""

from types import SimpleNamespace

import pytest
from core.llm import Answer, DecisionResult
from core.retrieval import Retrieved

from app.agent import prompts, task

APP_CONFIG = {
    "models": {
        "judge": {"provider": "gemini", "name": "gemini-decision-1"},
        "explain": {"provider": "gemini", "name": "gemini-3.6-flash", "max_output_tokens": 400},
    },
    "prompts": {"label": "evaluation"},
    "retrieval": {"top_k": 10, "search_depth": 20},
    "agent": {
        "void_threshold": 0.5,
        "basis_threshold": 0.5,
        "confidence_uncertain_below": 0.6,
        "max_retrieval_retries": 1,
        "explain_enabled": False,
    },
    "langfuse": {"enabled": False},
}

CLAUSE = {
    "clause_id": "agb-349",
    "title": "6 Haftung",
    "text": "Wir haften nur bei Vorsatz.",
    "topics": ["liability"],
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No Langfuse, no Postgres, no Qdrant, no Gemini."""
    prompts.clear_cache()
    monkeypatch.setattr(
        prompts,
        "load_settings",
        lambda: SimpleNamespace(langfuse_public_key="", langfuse_secret_key=""),
    )
    monkeypatch.setattr(
        task,
        "load_settings",
        lambda: SimpleNamespace(langfuse_public_key="", langfuse_secret_key=""),
    )
    monkeypatch.setattr(task, "index_hash", lambda: "index-test")
    yield
    prompts.clear_cache()


def test_run_metadata_identifies_the_run(monkeypatch):
    """The first Phase 4 run recorded only approach/split/items, so two runs
    with different prompts were indistinguishable in Langfuse."""
    meta = task.run_metadata(APP_CONFIG)

    assert meta["judge_model"] == "gemini-decision-1"
    assert meta["explain_model"] == "gemini-3.6-flash"
    assert meta["index_hash"] == "index-test"
    assert set(meta["prompt_fingerprints"]) == set(prompts.PROMPT_NAMES)
    assert len(meta["agent_version"]) == 16
    assert meta["retrieval_policy"]["top_k"] == 10
    assert meta["confidence_uncertain_below"] == 0.6
    assert meta["explain_enabled"] is False


def test_agent_version_in_metadata_tracks_the_prompts(monkeypatch):
    before = task.run_metadata(APP_CONFIG)["agent_version"]

    prompts.clear_cache()
    monkeypatch.setattr(prompts, "_local", lambda name: f"neu {name}")
    after = task.run_metadata(APP_CONFIG)["agent_version"]

    assert before != after


def test_explain_model_falls_back_to_the_judge_slot():
    no_explain = {**APP_CONFIG, "models": {"judge": APP_CONFIG["models"]["judge"]}}

    assert task._explain_model(no_explain)["name"] == "gemini-decision-1"


def test_agent_settings_fill_in_missing_config_keys():
    settings = task._agent_settings({"agent": {}})

    assert settings == task.DEFAULTS


def test_the_task_output_carries_every_contract_key(monkeypatch):
    chunk = Retrieved("bgb-309-7-a", "§ 309 Nr. 7 lit. a", "Gesetzestext", 0.9, 1)
    monkeypatch.setattr(task, "decide", lambda *a, **k: _ok_decision())

    run = task.make_task(
        app_config=APP_CONFIG,
        persist_verdicts=False,
        retriever=lambda *a, **k: [chunk],
    )
    result = run(item={"input": CLAUSE})

    for key in task.EMPTY_RESULT:
        assert key in result, f"the harness depends on {key!r}"
    assert result["verdict"] == "potentially_void"
    assert result["legal_basis"] == ["bgb-309-7-a"]
    assert result["candidates"] == ["bgb-309-7-a"]
    assert result["input_tokens"] == 120


def test_the_task_accepts_both_item_shapes(monkeypatch):
    """Langfuse hands back a dict for local data and a DatasetItem object for a
    published dataset."""
    chunk = Retrieved("bgb-309-7-a", "§ 309", "text", 0.9, 1)
    monkeypatch.setattr(task, "decide", lambda *a, **k: _ok_decision())

    run = task.make_task(
        app_config=APP_CONFIG,
        persist_verdicts=False,
        retriever=lambda *a, **k: [chunk],
    )
    as_dict = run(item={"input": CLAUSE})
    as_object = run(item=SimpleNamespace(input=CLAUSE))

    assert as_dict["verdict"] == as_object["verdict"] == "potentially_void"


def test_a_crash_inside_the_graph_becomes_an_error_result(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("qdrant unreachable")

    run = task.make_task(app_config=APP_CONFIG, persist_verdicts=False, retriever=explode)
    result = run(item={"input": CLAUSE})

    # A failed clause costs recall, it does not stop the run.
    assert result["verdict"] is None
    assert "RuntimeError" in result["error"]
    assert result["candidates"] == []


def test_make_task_does_not_touch_postgres_when_persistence_is_off(monkeypatch):
    called = []
    monkeypatch.setattr(task.verdicts, "ensure_table", lambda *a, **k: called.append(1))

    task.make_task(app_config=APP_CONFIG, persist_verdicts=False)

    assert called == []


def test_make_task_creates_the_table_once_when_persisting(monkeypatch):
    called = []
    monkeypatch.setattr(task.verdicts, "ensure_table", lambda *a, **k: called.append(1))

    task.make_task(app_config=APP_CONFIG, persist_verdicts=True)

    assert called == [1], "ensure_table belongs in setup, not in the per-clause write path"


def _ok_decision() -> DecisionResult:
    return DecisionResult(
        answers={
            "is_void": Answer(value=0.9, confidence=0.9, probabilities=None),
            "bgb-309-7-a": Answer(value=0.8, confidence=0.8, probabilities=None),
        },
        input_tokens=120,
        output_tokens=0,
        model="gemini-decision-1",
        error=None,
    )

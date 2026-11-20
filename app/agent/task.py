"""Wire the graph to providers and expose it as an evaluation task.

The signature matches `evals.baseline.task`, so the harness, the scorers and the
reports work on the agent without a separate adapter.
"""

from contextlib import suppress
from typing import Any

from core import langfuse_client, masking
from core.budget import RequestBudget
from core.config import index_config_hash, load_yaml
from core.hashing import content_hash
from core.llm import Completion, complete, decide
from core.paths import CONFIGS
from core.retrieval import Retrieved
from core.routing import classify_topic
from core.settings import load_settings

from app.agent import prompts, verdicts
from app.agent.graph import _observe, build_graph

# Defaults for keys configs/app.yaml does not carry yet. The thresholds are
# probabilities from the Decisions API, not model self-reports, so 0.5 is a
# genuine midpoint rather than a guess.
DEFAULTS = {
    "void_threshold": 0.5,
    "basis_threshold": 0.5,
    "confidence_uncertain_below": 0.6,
    "max_retrieval_retries": 1,
    "explain_enabled": True,
}

# The output contract the harness and the report writer depend on.
EMPTY_RESULT = {
    "verdict": None,
    "legal_basis": [],
    "candidates": [],
    "reason_de": "",
    "confidence": 0.0,
    "void_probability": None,
    "error": None,
    "input_tokens": 0,
    "output_tokens": 0,
}


def _agent_settings(app_config: dict[str, Any]) -> dict[str, Any]:
    agent = app_config.get("agent", {})
    return {key: agent.get(key, default) for key, default in DEFAULTS.items()}


def _explain_model(app_config: dict[str, Any]) -> dict[str, Any]:
    """The text model for `reason_de`.

    The Decisions API generates no text, so the reason needs a second, ordinary
    model. Falls back to the judge slot when no `explain` slot is configured.
    """
    models = app_config.get("models", {})
    return models.get("explain") or models.get("judge", {})


def index_hash() -> str:
    """Lineage of the index the agent is reading, for the agent version."""
    return index_config_hash(
        load_yaml(CONFIGS / "kb.yaml"), load_yaml(CONFIGS / "kb_paragraphs.yaml")
    )


def run_metadata(app_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything needed to tell two runs apart after the fact.

    Run metadata includes the approach, split, model, prompt, and index details so
    made two runs with different prompts indistinguishable in Langfuse.
    """
    app_config = app_config or load_yaml(CONFIGS / "app.yaml")
    label = app_config.get("prompts", {}).get("label", "production")
    fingerprints = prompts.fingerprint(label)
    settings = _agent_settings(app_config)
    return {
        "approach": "agentic RAG, Gemini Decisions",
        "judge_model": app_config.get("models", {}).get("judge", {}).get("name"),
        "explain_model": _explain_model(app_config).get("name"),
        "prompt_label": label,
        "prompt_fingerprints": fingerprints,
        "index_hash": index_hash(),
        "agent_version": verdicts.agent_version(app_config, index_hash(), fingerprints),
        "retrieval_policy": app_config.get("retrieval", {}),
        **settings,
    }


def make_task(
    *,
    budget: RequestBudget | None = None,
    client: Any = None,
    app_config: dict[str, Any] | None = None,
    persist_verdicts: bool = True,
    retriever: Any = None,
):
    """Build the task function. The graph is compiled once, not per clause."""
    app_config = app_config or load_yaml(CONFIGS / "app.yaml")
    settings_obj = load_settings()
    label = app_config.get("prompts", {}).get("label", "production")
    agent_settings = _agent_settings(app_config)

    judge_model = app_config.get("models", {}).get("judge", {})
    router_model = app_config.get("models", {}).get("router", {})
    explain_model = _explain_model(app_config)
    fingerprints = prompts.fingerprint(label)
    version = verdicts.agent_version(app_config, index_hash(), fingerprints)

    tracer = None
    if (
        app_config.get("langfuse", {}).get("enabled", True)
        and settings_obj.langfuse_public_key
        and settings_obj.langfuse_secret_key
    ):
        tracer = langfuse_client.client()

    if persist_verdicts:
        verdicts.ensure_table()

    def decider(text: str, questions: dict[str, dict], model: dict[str, Any]):
        return decide(text, questions, model or judge_model, budget=budget, client=client)

    def router(text: str) -> str:
        try:
            return classify_topic(
                text,
                model=router_model.get("name", "gemini-decision-1"),
                budget=budget,
                client=client,
            )
        except Exception:  # noqa: BLE001 - broad retrieval is safer than dropping a clause
            return ""

    router_callable = router if router_model else None

    def explainer(clause: dict[str, Any], cited: list[Retrieved]) -> Completion:
        evidence = "\n\n".join(f"{c.citation}\n{c.text}" for c in cited)
        user = (
            f"Abschnitt: {clause.get('title') or '(ohne Überschrift)'}\n"
            f"Klausel:\n{clause['text']}\n\n"
            f"Maßgebliche Normen:\n{evidence}"
        )
        return complete(
            prompts.load("explain", label=label).text,
            user,
            prompts.reason_schema(),
            explain_model,
            budget=budget,
            client=client,
        )

    # The graph hands `persist` a record keyed by the eval clause id; the table is
    # keyed by the hash of the text, so `run` notes each clause's hash on the way in.
    hashes: dict[str, str] = {}

    def persist(record: Any) -> None:
        verdicts.save(record, version, hashes[record.clause_id])

    # `retriever` is injectable for the same reason `client` is: build_graph
    # binds the real `retrieve` as a default argument, so patching the module
    # afterwards would not stop a test reaching Qdrant.
    graph_kwargs: dict[str, Any] = {}
    if retriever is not None:
        graph_kwargs["retriever"] = retriever

    compiled = build_graph(
        router=router_callable,
        decider=decider,
        explainer=explainer,
        persist=persist if persist_verdicts else None,
        policy=app_config.get("retrieval", {}),
        models={"judge": judge_model, "explain": explain_model},
        prompt_label=label,
        tracer=tracer,
        **agent_settings,
        **graph_kwargs,
    )

    def run(*, item: Any, **_: Any) -> dict[str, Any]:
        clause = item["input"] if isinstance(item, dict) else item.input
        hashes[clause["clause_id"]] = content_hash(clause["text"])
        # Use one masking scope per clause, matching the API judge:
        # clause text first, heading second, answers restored only from the clause text.
        with masking.scope(
            None,
            hashes[clause["clause_id"]],
            prime=[clause.get("text"), clause.get("title")],
            restore_from=[clause.get("text") or ""],
        ):
            return _run_clause(clause)

    def _run_clause(clause: dict[str, Any]) -> dict[str, Any]:
        try:
            with _observe(
                tracer,
                "agb-agent",
                as_type="agent",
                input={
                    "clause_id": clause.get("clause_id"),
                    "title": clause.get("title"),
                    "text": clause.get("text"),
                },
                metadata={"agent_version": version, "prompt_label": label},
            ) as observation:
                state = compiled.invoke({"clause": dict(clause), "retries": 0})
                result = {**EMPTY_RESULT, **state["result"]}
                if observation is not None:
                    # Tracing is never load-bearing.
                    with suppress(Exception):
                        observation.update(output=result)
            return result
        except Exception as exc:  # noqa: BLE001 - a failed clause is data, not a crash
            return {**EMPTY_RESULT, "error": f"{type(exc).__name__}: {exc}"}

    return run

"""Guards the agent runner's glue: slice selection, metadata, budget.

The first Phase 4 run recorded only {approach, split, items}, which left two
runs after a prompt change indistinguishable in Langfuse. These assert the
metadata is actually rich and that both runners slice identically - the v1
comparison is only valid if they do.
"""

from core.config import load_yaml
from core.paths import CONFIGS

from evals.run_baseline import REQUIRED_KEYS, build_budget, missing_keys, slice_config


def test_the_committed_config_has_every_key_the_runners_read():
    # A rename now fails `make check` instead of a paid run partway through.
    assert missing_keys(load_yaml(CONFIGS / "evals.yaml"), REQUIRED_KEYS) == []


def test_slice_config_is_shared_so_both_sides_slice_identically():
    config = load_yaml(CONFIGS / "evals.yaml")

    spec = slice_config(config)

    assert spec["slice"] == "stratified"
    assert spec["non_void"] == 39
    assert spec["seed"] == 0


def test_slice_config_honours_an_override():
    config = load_yaml(CONFIGS / "evals.yaml")

    assert slice_config(config, "first:5")["slice"] == "first:5"


def test_budget_comes_from_config_and_paces_under_the_free_tier_rpm():
    config = load_yaml(CONFIGS / "evals.yaml")

    budget = build_budget(config)

    # Gemini free tier is 200 RPM / 10,000 RPD; pace just under so a burst
    # cannot trip it.
    assert budget.max_requests == 10000
    assert budget.rpm is not None and budget.rpm < 200
    assert budget.spent() == 0


def test_agent_run_metadata_identifies_the_run():
    from app.agent.task import run_metadata

    metadata = run_metadata()

    # Enough to reconstruct which agent produced a number.
    assert metadata["agent_version"]
    assert metadata["judge_model"] == "gemini-decision-1"
    assert metadata["prompt_fingerprints"]
    assert metadata["index_hash"]
    assert "void_threshold" in metadata
    assert "top_k" in metadata["retrieval_policy"]


def test_agent_metadata_moves_when_a_threshold_moves():
    from app.agent.task import run_metadata

    base = load_yaml(CONFIGS / "app.yaml")
    shifted = {**base, "agent": {**base["agent"], "void_threshold": 0.9}}

    assert run_metadata(base)["agent_version"] != run_metadata(shifted)["agent_version"]

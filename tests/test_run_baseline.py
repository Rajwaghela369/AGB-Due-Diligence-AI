"""Guards the glue layer.

Both of the first two Phase 3 failures were stale config reads in
run_baseline.py - the one module with no tests, because it builds an API client
and was assumed to need the network. The config checks below need neither.
"""

import pytest
from core.config import load_yaml
from core.paths import CONFIGS

from evals.run_baseline import (
    REQUIRED_EVAL_KEYS,
    REQUIRED_KEYS,
    ConfigError,
    load_config,
    missing_keys,
    run_config,
)

# v1 runs the baseline on Gemini so both sides of the comparison share a
# provider. `name` rather than `model`, because that is the key core.llm reads.
MODEL = {
    "provider": "gemini",
    "name": "gemini-3.8-flash",
    "max_output_tokens": 400,
}


def test_the_real_configs_satisfy_every_key_this_module_reads():
    # The point of the whole file: renaming a key in YAML now fails
    # `make check` instead of a live run.
    assert missing_keys(load_yaml(CONFIGS / "evals.yaml"), REQUIRED_KEYS) == []
    assert missing_keys(load_yaml(CONFIGS / "eval.yaml"), REQUIRED_EVAL_KEYS) == []


def test_load_config_accepts_the_committed_configs():
    config, eval_config = load_config()

    assert config["baseline"]["name"]
    assert eval_config["splits"]["dev"] == "train"


def test_missing_keys_reports_every_absence_not_just_the_first():
    # One run should surface all of them; fixing them one error at a time is
    # how a config rename turns into four failed runs.
    absent = missing_keys({"baseline": {"model": "x"}}, REQUIRED_KEYS)

    assert "baseline.max_output_tokens" in absent
    assert "run.experiment" in absent
    assert "dataset.held_out" in absent
    assert "baseline.model" not in absent


def test_missing_keys_handles_a_leaf_where_a_dict_was_expected():
    # `run: 8` instead of `run: {max_concurrency: 8}` must not raise.
    absent = missing_keys({"run": 8}, ("run.max_concurrency",))

    assert absent == ["run.max_concurrency"]


def test_load_config_names_the_missing_paths(monkeypatch):
    monkeypatch.setattr(
        "evals.run_baseline.load_yaml",
        lambda path: {"baseline": {"model": "x"}} if path.name == "evals.yaml" else {},
    )

    with pytest.raises(ConfigError, match="evals.yaml:run.experiment"):
        load_config()


def test_run_config_spreads_the_model_block():
    # Cherry-picking keys here is what broke: `temperature` was removed from
    # the YAML and this dict still asked for it.
    config = run_config(MODEL, "abc123def456", "dev", 419)

    assert config["name"] == "gemini-3.8-flash"
    assert config["provider"] == "gemini"
    assert config["max_output_tokens"] == 400
    assert config["prompt_sha"] == "abc123def456"
    assert config["split"] == "dev"
    assert config["items"] == 419
    assert config["approach"] == "one-shot, no retrieval"


def test_run_config_survives_an_unknown_model_setting():
    config = run_config({**MODEL, "thinking_effort": "low"}, "x", "dev", 1)

    assert config["thinking_effort"] == "low"


def test_run_config_does_not_reference_a_removed_setting():
    # temperature is not a parameter of the Anthropic API version in use.
    assert "temperature" not in run_config(MODEL, "x", "dev", 1)


def test_the_configured_model_matches_what_the_tests_assume():
    # Catches a model rename in YAML that the baseline tests would miss.
    assert load_yaml(CONFIGS / "evals.yaml")["baseline"]["name"] == MODEL["name"]

"""Phase 3 entry point: run the one-shot baseline and record its number.

uv run python -m evals.run_baseline              # dev half, 419 clauses
uv run python -m evals.run_baseline --split held_out   # the gate number

Every value this module needs from YAML is declared in REQUIRED_KEYS and checked
up front. A config rename then fails `make check` with the missing path named,
instead of failing a live run partway through - which is how both of the first
two bugs here were found.
"""

import argparse
from datetime import UTC, datetime
from typing import Any

from anthropic import Anthropic
from core.config import load_yaml
from core.paths import CONFIGS, PROCESSED, REPORTS
from core.settings import load_settings

from evals import baseline
from evals.datasets import load
from evals.harness import RunResult, run

# Dotted paths into configs/evals.yaml that this module reads.
REQUIRED_KEYS = (
    "baseline.model",
    "baseline.max_output_tokens",
    "run.experiment",
    "run.max_concurrency",
    "run.default_split",
    "scoring.uncertain_counts_as_void",
    "dataset.held_out",
)

# Paths into configs/eval.yaml, which P2 owns.
REQUIRED_EVAL_KEYS = ("splits.dev",)


class ConfigError(RuntimeError):
    """A key this module needs is absent from the YAML."""


def missing_keys(config: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """Every absent path, not just the first - one run should surface them all."""
    absent = []
    for path in required:
        node: Any = config
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                absent.append(path)
                break
            node = node[part]
    return absent


def load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """The harness config and P2's config, validated before anything runs."""
    config = load_yaml(CONFIGS / "evals.yaml")
    eval_config = load_yaml(CONFIGS / "eval.yaml")

    absent = [
        *(f"evals.yaml:{p}" for p in missing_keys(config, REQUIRED_KEYS)),
        *(f"eval.yaml:{p}" for p in missing_keys(eval_config, REQUIRED_EVAL_KEYS)),
    ]
    if absent:
        raise ConfigError("missing config keys: " + ", ".join(absent))

    return config, eval_config


def run_config(model: dict[str, Any], fingerprint: str, split: str, items: int) -> dict[str, Any]:
    """Metadata describing the run, for Langfuse and for the report.

    The model block is spread rather than cherry-picked, so this records what
    is actually configured and cannot break when a model setting is renamed.
    """
    return {
        "approach": "one-shot, no retrieval",
        **model,
        "prompt_sha": fingerprint,
        "split": split,
        "items": items,
    }


def run_baseline(split: str, limit: int | None = None) -> RunResult:
    config, eval_config = load_config()
    model = config["baseline"]

    settings = load_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; copy .env.example to .env")

    items = load(
        split,
        dev_path=PROCESSED / "splits" / f"{eval_config['splits']['dev']}.parquet",
        dataset_name=config["dataset"]["held_out"],
    )
    if limit:
        items = items[:limit]

    client = Anthropic(api_key=settings.anthropic_api_key)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")

    return run(
        name=config["run"]["experiment"],
        run_name=f"baseline-{split}-{stamp}",
        items=items,
        task=baseline.task(client, model),
        split=split,
        config=run_config(model, baseline.prompt_fingerprint(), split, len(items)),
        report_path=REPORTS / f"p3_baseline_{split}.json",
        # A limited run must not attach to the published dataset, or the
        # experiment would look like a full run with items missing.
        dataset_name=(config["dataset"]["held_out"] if split == "held_out" and not limit else None),
        max_concurrency=config["run"]["max_concurrency"],
        uncertain_counts_as_void=config["scoring"]["uncertain_counts_as_void"],
    )


def main() -> None:
    config, _ = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--split",
        choices=("dev", "held_out"),
        default=config["run"]["default_split"],
        help="dev for iteration, held_out for the gate number",
    )
    ap.add_argument(
        "--limit",
        type=int,
        help="first N clauses only, for a cheap smoke run",
    )
    args = ap.parse_args()

    result = run_baseline(args.split, args.limit)
    print(
        f"\n{result.run_name}: void F1 {result.metrics['void_f1']:.3f} "
        f"(P {result.metrics['void_precision']:.3f} / R {result.metrics['void_recall']:.3f}) "
        f"over {result.items} {result.split} clauses"
    )
    print(f"report: {result.report_path}")


if __name__ == "__main__":
    main()

"""The one-shot baseline: the number the agent has to beat.

    uv run python -m evals.run_baseline                    # the v1 stratified slice
    uv run python -m evals.run_baseline --slice all        # the full 419
    uv run python -m evals.run_baseline --slice first:5    # a cheap smoke run

v1 re-runs this on Gemini over the stratified 70-clause slice, because Phase 3's
F1 0.276 was measured on Haiku 4.5 across all 419 and is therefore not a valid
target for a different provider on a different slice. Both sides of a comparison
have to be measured the same way.

The slice helpers here are shared with `run_agent`, so the two runs cannot drift
apart. Every value read from YAML is declared in REQUIRED_KEYS and checked up
front: a config rename then fails `make check` with the missing path named,
instead of failing a live run partway through - which is how both of the first
two bugs here were found.
"""

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.budget import RequestBudget
from core.config import load_yaml
from core.paths import CONFIGS, PROCESSED, REPORTS

from evals import baseline
from evals.datasets import EvalItem, load, select, slice_label
from evals.harness import RunResult, run

# Dotted paths into configs/evals.yaml that this module reads. Declared because
# a stale key broke two live runs: a rename now fails `make check` with the
# path named instead of failing partway through a paid run.
REQUIRED_KEYS = (
    "baseline.provider",
    "baseline.name",
    "baseline.max_output_tokens",
    "run.experiment",
    "run.max_concurrency",
    "run.default_split",
    "run.slice",
    "run.non_void",
    "run.seed",
    "run.request_budget",
    "run.rpm",
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


def slice_config(config: dict[str, Any], spec: str | None = None) -> dict[str, Any]:
    """Slice settings both runners share, so the two sides stay comparable."""
    return {
        "slice": spec or config["run"]["slice"],
        "non_void": int(config["run"]["non_void"]),
        "seed": int(config["run"]["seed"]),
    }


def build_budget(config: dict[str, Any]) -> RequestBudget:
    """Stop deliberately at the quota rather than degrading mid-run."""
    return RequestBudget(
        max_requests=int(config["run"]["request_budget"]),
        rpm=int(config["run"]["rpm"]),
    )


def choose_items(
    split: str, config: dict[str, Any], eval_config: dict[str, Any], spec: str
) -> tuple[list[EvalItem], str]:
    """Load a split and apply the slice spec, returning the items and a label."""
    items = load(
        split,
        dev_path=PROCESSED / "splits" / f"{eval_config['splits']['dev']}.parquet",
        dataset_name=config["dataset"]["held_out"],
    )
    chosen = select(
        items,
        spec=spec,
        non_void=int(config["run"]["non_void"]),
        seed=int(config["run"]["seed"]),
    )
    return chosen, slice_label(spec, len(chosen))


def report_path_for(prefix: str, split: str, label: str) -> Path:
    """`p3_baseline_dev70.json` - the slice is in the name so slices never mix."""
    return REPORTS / f"{prefix}_{label if split == 'dev' else split}.json"


def run_baseline(split: str, slice_spec: str | None = None) -> RunResult:
    config, eval_config = load_config()
    model = config["baseline"]
    spec = slice_spec or config["run"]["slice"]

    items, label = choose_items(split, config, eval_config, spec)
    budget = build_budget(config)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")

    return run(
        name=config["run"]["experiment"],
        run_name=f"baseline-{label if split == 'dev' else split}-{stamp}",
        items=items,
        task=baseline.task(model, budget=budget),
        split=split,
        config={
            **run_config(model, baseline.prompt_fingerprint(), split, len(items)),
            **slice_config(config, spec),
        },
        report_path=report_path_for("p3_baseline", split, label),
        # A sliced run must not attach to the published dataset, or the
        # experiment would look like a full run with items missing.
        dataset_name=(
            config["dataset"]["held_out"] if split == "held_out" and spec == "all" else None
        ),
        max_concurrency=config["run"]["max_concurrency"],
        uncertain_counts_as_void=config["scoring"]["uncertain_counts_as_void"],
        budget=budget,
    )


def print_summary(result: RunResult) -> None:
    """Raw and weighted side by side, so neither can be mistaken for the other."""
    m = result.metrics
    print(
        f"\n{result.run_name}: {result.items} {result.split} clauses, "
        f"{result.requests_spent} requests"
    )
    print(
        f"  raw      P {m['void_precision']:.3f}  R {m['void_recall']:.3f}  F1 {m['void_f1']:.3f}"
    )
    print(
        f"  weighted P {m['void_precision_weighted']:.3f}  R {m['void_recall']:.3f}  "
        f"F1 {m['void_f1_weighted']:.3f}   <- corrected to the 7.4% base rate"
    )
    print(
        f"  grounding {m['grounding_rate']:.3f} over {int(m['void_calls'])} void calls, "
        f"errors {int(m['errors'])}, uncertain {int(m['uncertain'])}"
    )
    print(f"  report: {result.report_path}")


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
        "--slice",
        dest="slice_spec",
        default=None,
        help='"stratified" (v1 default), "all", or "first:N" for a cheap smoke run',
    )
    args = ap.parse_args()

    print_summary(run_baseline(args.split, args.slice_spec))


if __name__ == "__main__":
    main()

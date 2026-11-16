"""Run Phase 4's graph through the permanent evaluation harness.

    uv run python -m evals.run_agent                      # the v1 stratified slice
    uv run python -m evals.run_agent --slice first:5      # a cheap smoke run

The agent and the baseline go through the same harness, over the same slice,
on the same provider - that is what makes the two numbers comparable. The
metadata recorded here is deliberately rich: the first Phase 4 run logged only
{approach, split, items}, which left two runs after a prompt change
indistinguishable in Langfuse.
"""

import argparse
from datetime import UTC, datetime

from app.agent.task import make_task, run_metadata
from evals.harness import RunResult, run
from evals.run_baseline import (
    build_budget,
    choose_items,
    load_config,
    print_summary,
    report_path_for,
    slice_config,
)


def run_agent(split: str = "dev", slice_spec: str | None = None) -> RunResult:
    config, eval_config = load_config()
    spec = slice_spec or config["run"]["slice"]

    items, label = choose_items(split, config, eval_config, spec)
    budget = build_budget(config)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")

    return run(
        name=config["run"]["experiment"],
        run_name=f"agent-{label if split == 'dev' else split}-{stamp}",
        items=items,
        task=make_task(budget=budget),
        split=split,
        config={
            "approach": "agentic RAG",
            **run_metadata(),
            **slice_config(config, spec),
            "split": split,
            "items": len(items),
        },
        report_path=report_path_for("p4_agent", split, label),
        dataset_name=(
            config["dataset"]["held_out"] if split == "held_out" and spec == "all" else None
        ),
        max_concurrency=config["run"]["max_concurrency"],
        uncertain_counts_as_void=config["scoring"]["uncertain_counts_as_void"],
        budget=budget,
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
        "--slice",
        dest="slice_spec",
        default=None,
        help='"stratified" (v1 default), "all", or "first:N" for a cheap smoke run',
    )
    args = ap.parse_args()

    print_summary(run_agent(args.split, args.slice_spec))


if __name__ == "__main__":
    main()

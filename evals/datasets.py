"""The two places evaluation data comes from, and why they differ.

The held-out half lives in Langfuse, published by P2 step 2.5. The dev half
lives only in `data/processed/splits/train.parquet` on disk: its labels were
never published anywhere, because nothing but the harness needs them.

Both are shaped into the same experiment item, so a task function cannot tell
which half it is being run against. `test_datasets.py` asserts that.
"""

import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from core import langfuse_client
from core.logging import get_logger

log = get_logger("evals.data")

Split = Literal["dev", "held_out"]

# What the agent is handed. Must stay identical to the `input` published to
# Langfuse by pipelines/p2_eval/s5_publish.py, or dev and held-out runs stop
# being comparable.
INPUT_KEYS = ("clause_id", "title", "text", "topics")


@dataclass(frozen=True)
class EvalItem:
    """One clause to judge, with the answer kept beside it rather than inside it."""

    id: str
    input: dict[str, Any]
    expected_output: dict[str, Any]
    metadata: dict[str, Any]

    @property
    def label_void(self) -> bool:
        return bool(self.expected_output["label_void"])

    def as_local(self) -> dict[str, Any]:
        """The shape langfuse.run_experiment accepts for local data."""
        return {
            "input": self.input,
            "expected_output": self.expected_output,
            "metadata": {**self.metadata, "clause_id": self.id},
        }


def _compose_title(section_no: object, section_title: object) -> str:
    parts = [str(p) for p in (section_no, section_title) if isinstance(p, str) and p]
    return " ".join(parts)


def load_dev(path: Path) -> list[EvalItem]:
    """The development half, straight off disk. Labels never leave the machine."""
    frame = pd.read_parquet(path)
    items = [
        EvalItem(
            id=row["clause_id"],
            input={
                "clause_id": row["clause_id"],
                "title": _compose_title(row["section_no"], row["section_title"]),
                "text": row["text"],
                "topics": list(row["topics"]),
            },
            expected_output={"label_void": bool(row["label_void"])},
            metadata={
                "split": "dev",
                "contract_id": int(row["contract_id"]),
                "is_fragment": bool(row["is_fragment"]),
                "foreign_law_choice": bool(row["foreign_law_choice"]),
            },
        )
        for row in frame.to_dict("records")
    ]
    log.info(
        "loaded %d dev clauses (%d void) from %s",
        len(items),
        sum(i.label_void for i in items),
        path.name,
    )
    return items


def load_held_out(dataset_name: str) -> list[EvalItem]:
    """The held-out half, from the Langfuse dataset P2 published."""
    dataset = langfuse_client.client().get_dataset(dataset_name)
    items = [
        EvalItem(
            id=item.id,
            input=dict(item.input or {}),
            expected_output=dict(item.expected_output or {}),
            metadata=dict(item.metadata or {}),
        )
        for item in dataset.items
    ]
    log.info(
        "loaded %d held-out clauses (%d void) from Langfuse dataset %s",
        len(items),
        sum(i.label_void for i in items),
        dataset_name,
    )
    return items


def load(split: Split, dev_path: Path, dataset_name: str) -> list[EvalItem]:
    if split == "dev":
        return load_dev(dev_path)
    return load_held_out(dataset_name)


def stratified(items: list[EvalItem], non_void: int = 39, seed: int = 0) -> list[EvalItem]:
    """All void clauses plus a seeded random sample of non-void ones.

    Two metrics behave differently under sampling, which is the whole reason
    this exists:

    Recall is base-rate independent - it is TP/(TP+FN), and only void clauses
    appear in it. Taking *every* void clause therefore buys the sharpest recall
    the dev half can give, for the fewest requests. A 200-clause contiguous
    slice holds 22 void; this holds all 31.

    Precision is not. It moves entirely with the base rate: at 44% void a model
    that flags everything scores 0.44, where in production (7.4% void) the same
    model scores 0.074. Tuning on raw precision from a void-dense slice would
    push the model toward over-flagging, which is already the baseline's core
    failure (121 void calls to find 21). So each item carries a
    `sample_weight`, and `scorers.weighted_metrics` uses it to correct
    precision back to the true base rate.

    The non-void stratum is sampled randomly rather than taken as the first N,
    because the first 39 non-void clauses come from 15 of 84 shops while a
    seeded sample spans 33 - and the weighting is only unbiased if the stratum
    represents the population it stands for. `seed` keeps it reproducible.
    """
    void = sorted((i for i in items if i.label_void), key=lambda i: i.id)
    rest = sorted((i for i in items if not i.label_void), key=lambda i: i.id)

    if non_void > len(rest):
        raise ValueError(f"asked for {non_void} non-void clauses, only {len(rest)} exist")

    chosen = random.Random(seed).sample(rest, non_void)
    # Void is sampled at 100%, so it stands only for itself.
    weight = len(rest) / non_void if non_void else 0.0

    tagged = [
        *(
            replace(i, metadata={**i.metadata, "stratum": "void", "sample_weight": 1.0})
            for i in void
        ),
        *(
            replace(i, metadata={**i.metadata, "stratum": "non_void", "sample_weight": weight})
            for i in chosen
        ),
    ]
    log.info(
        "stratified %d clauses: %d void (weight 1.0) + %d of %d non-void (weight %.4f)",
        len(tagged),
        len(void),
        non_void,
        len(rest),
        weight,
    )
    return sorted(tagged, key=lambda i: i.id)


def select(
    items: list[EvalItem], spec: str = "all", non_void: int = 39, seed: int = 0
) -> list[EvalItem]:
    """Apply a slice spec: "all", "stratified", or "first:N".

    Items not drawn by `stratified` still get a `sample_weight` of 1.0, so the
    weighted and unweighted metrics agree on an unsampled run rather than one
    of them silently reading a missing key as zero.
    """
    if spec == "stratified":
        return stratified(items, non_void=non_void, seed=seed)

    if spec.startswith("first:"):
        count = int(spec.split(":", 1)[1])
        chosen = items[:count]
    elif spec == "all":
        chosen = list(items)
    else:
        raise ValueError(f"unknown slice spec {spec!r}; use all, stratified or first:N")

    return [
        replace(
            i,
            metadata={
                **i.metadata,
                "stratum": "void" if i.label_void else "non_void",
                "sample_weight": 1.0,
            },
        )
        for i in chosen
    ]


def slice_label(spec: str, items: int) -> str:
    """Short name for report filenames, e.g. "dev70" - keeps slices distinct."""
    return f"dev{items}" if spec != "all" else "dev"

"""The two places evaluation data comes from, and why they differ.

The held-out half lives in Langfuse, published by P2 step 2.5. The dev half
lives only in `data/processed/splits/train.parquet` on disk: its labels were
never published anywhere, because nothing but the harness needs them.

Both are shaped into the same experiment item, so a task function cannot tell
which half it is being run against. `test_datasets.py` asserts that.
"""

from dataclasses import dataclass
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

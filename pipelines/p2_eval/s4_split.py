"""2.4 Split: attach the authors' train/test assignment to the scoped clauses.

Nothing here trains anything. The assignment is reused so the development half
and the held-out half are the same ones the AGB-DE paper used, which keeps our
numbers comparable to theirs and keeps prompt iteration off the clauses we
report on.
"""

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from core.logging import get_logger

log = get_logger("p2.split")


class UnassignedClauses(RuntimeError):
    """An in-scope clause belongs to neither split.

    Hard failure, for the reason P1 fails on an unresolved paragraph: a clause
    that silently lands in neither half would either vanish from the evaluation
    or leak into the held-out set, and both are discovered far too late.
    """


@dataclass(frozen=True)
class SplitResult:
    paths: dict[str, Path]
    counts: dict[str, int]
    void: dict[str, int]


def split_clauses(
    scope_path: Path,
    splits_path: Path,
    dev: str,
    held_out: str,
    out_dir: Path,
) -> SplitResult:
    scoped = pd.read_parquet(scope_path)
    assignment = pd.read_parquet(splits_path)

    merged = scoped.merge(
        assignment.rename(columns={"id": "source_id"}), on="source_id", how="left"
    )

    unassigned = merged[merged.split.isna()]
    if not unassigned.empty:
        ids = ", ".join(str(i) for i in unassigned.source_id.head(10))
        raise UnassignedClauses(
            f"{len(unassigned)} of {len(merged)} in-scope clauses have no split "
            f"assignment in {splits_path.name}: {ids}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    paths, counts, void = {}, {}, {}
    for name in (dev, held_out):
        part = merged[merged.split == name].drop(columns=["split"])
        path = out_dir / f"{name}.parquet"
        part.to_parquet(path, index=False)
        paths[name], counts[name], void[name] = path, len(part), int(part.label_void.sum())
        log.info("%s: %d clauses, %d void -> %s", name, counts[name], void[name], path.name)

    return SplitResult(paths=paths, counts=counts, void=void)

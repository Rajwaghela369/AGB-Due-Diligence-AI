"""Run step 2.4 and print how the split attaches, what it guards, and what it writes.

Inspection only. Reads data/processed/agb_scope_v1.parquet (run scripts/show_s3.py
first) and writes data/processed/splits/{train,test}.parquet. Safe to re-run.
"""

import pandas as pd
from core.config import load_yaml
from core.paths import CONFIGS, PROCESSED, RAW, ROOT

from pipelines.p2_eval.s4_split import UnassignedClauses, split_clauses


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n {title}\n{'=' * 78}")


def demo_guard(tmp_dir, scoped: pd.DataFrame) -> None:
    """Prove UnassignedClauses fires, using a deliberately incomplete assignment."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    scope_file = tmp_dir / "scope.parquet"
    splits_file = tmp_dir / "splits.parquet"

    sample = scoped.head(3).copy()
    scoped.head(3).to_parquet(scope_file, index=False)
    # Assign only the first of the three.
    pd.DataFrame({"id": [int(sample.source_id.iloc[0])], "split": ["train"]}).to_parquet(
        splits_file, index=False
    )

    try:
        split_clauses(scope_file, splits_file, "train", "test", tmp_dir)
        print("  NO EXCEPTION - the guard did not fire, which is a bug")
    except UnassignedClauses as exc:
        print(f"  UnassignedClauses raised, as it should:\n    {exc}")


def main() -> None:
    pd.set_option("display.width", 200)
    config = load_yaml(CONFIGS / "eval.yaml")
    splits_cfg, gate, commit = config["splits"], config["gate"], config["source"]["commit"]
    dev, held_out = splits_cfg["dev"], splits_cfg["held_out"]

    head("INPUT - which half is which, from configs/eval.yaml")
    print(f"  dev (iterate here)      : {dev!r}")
    print(f"  held_out (report here)  : {held_out!r}")
    print("  nothing is trained; the split keeps prompt tuning off the reported half")
    print(
        f"\n  gate: dev {gate['dev_clauses']}/{gate['dev_void']} void, "
        f"held_out {gate['held_out_clauses']}/{gate['held_out_void']} void"
    )

    scope_path = PROCESSED / "agb_scope_v1.parquet"
    splits_path = RAW / "agb-de" / f"splits_{commit[:8]}.parquet"
    scoped = pd.read_parquet(scope_path)
    assignment = pd.read_parquet(splits_path)

    head("THE MERGE - left join on source_id, never an inner join")
    print(f"  scoped clauses            : {len(scoped)}")
    print(f"  assignment rows           : {len(assignment)}")
    overlap = scoped.source_id.isin(assignment.id).sum()
    print(f"  scoped ids found in it    : {overlap}")
    print(f"  would an inner join lose? : {len(scoped) - int(overlap)} rows")
    print("  -> a left join keeps all 523 and the null check names anything missing")

    head("THE GUARD - UnassignedClauses, demonstrated on a rigged input")
    demo_guard(PROCESSED / "_guard_demo", scoped)

    head("FULL STEP - split_clauses()")
    result = split_clauses(scope_path, splits_path, dev, held_out, PROCESSED / "splits")
    print("\n  SplitResult:")
    for name in (dev, held_out):
        print(
            f"    {name:6} {result.counts[name]:>4} clauses, {result.void[name]:>3} void"
            f"  -> {result.paths[name].relative_to(ROOT)}"
        )

    head("RECONCILIATION - the halves must account for every scoped clause")
    total = sum(result.counts.values())
    total_void = sum(result.void.values())
    print(
        f"  {result.counts[dev]} + {result.counts[held_out]} = {total}  "
        f"(scoped: {len(scoped)})  match: {total == len(scoped)}"
    )
    print(
        f"  {result.void[dev]} + {result.void[held_out]} = {total_void}  "
        f"(scoped void: {int(scoped.label_void.sum())})  "
        f"match: {total_void == int(scoped.label_void.sum())}"
    )

    head("VOID RATE PER HALF - is the positive class balanced across them?")
    for name in (dev, held_out):
        rate = result.void[name] / result.counts[name]
        print(f"  {name:6} {result.void[name]:>3} / {result.counts[name]:>4} = {rate:.1%}")

    head("CONTRACT OVERLAP - do the same shops appear in both halves?")
    halves = {name: pd.read_parquet(result.paths[name]) for name in (dev, held_out)}
    dev_contracts = set(halves[dev].contract_id)
    held_contracts = set(halves[held_out].contract_id)
    shared = dev_contracts & held_contracts
    print(f"  contracts in {dev:6}       : {len(dev_contracts)}")
    print(f"  contracts in {held_out:6}       : {len(held_contracts)}")
    print(f"  in BOTH                   : {len(shared)}")
    if shared:
        print("  -> the authors split by clause, not by shop. Clauses from one shop")
        print("     sit on both sides, so wording learned on the dev half can recur")
        print("     in the held-out half. Worth recording before Phase 4.")
        print(f"     shared contract ids: {sorted(shared)[:15]}")

    head("FLAGS PER HALF")
    for name in (dev, held_out):
        part = halves[name]
        print(
            f"  {name:6} fragments={int(part.is_fragment.sum())}  "
            f"foreign_law={int(part.foreign_law_choice.sum())}  "
            f"foreign_law_void={int(part[part.foreign_law_choice].label_void.sum())}"
        )

    head("COLUMNS WRITTEN (the label still travels; only publish forbids it)")
    print(f"  {list(halves[dev].columns)}")

    head("GATE CHECK (asserted by the flow in 2.7)")
    for name, got, want in (
        ("dev_clauses", result.counts[dev], gate["dev_clauses"]),
        ("dev_void", result.void[dev], gate["dev_void"]),
        ("held_out_clauses", result.counts[held_out], gate["held_out_clauses"]),
        ("held_out_void", result.void[held_out], gate["held_out_void"]),
    ):
        print(f"  {'PASS' if got == want else 'FAIL'}  {name:18} got {got:4}  want {want:4}")


if __name__ == "__main__":
    main()

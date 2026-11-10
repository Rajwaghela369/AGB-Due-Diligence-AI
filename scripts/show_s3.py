"""Run step 2.3 and print how the scope filter narrows, flags, and drops nothing.

Inspection only. Reads data/interim/agb_clauses.parquet (run scripts/show_s2.py
first) and writes data/processed/agb_scope_v1.parquet. Safe to re-run.
"""

from collections import Counter

import pandas as pd
from core.config import load_yaml
from core.paths import CONFIGS, INTERIM, PROCESSED, ROOT

from pipelines.p2_eval.s3_filter_scope import (
    _FOREIGN_LAW,
    APPLICABLE_LAW_TOPIC,
    filter_scope,
    foreign_law_contracts,
)

# Each case is one thing the detector has to get right.
REGEX_CASES = (
    ("französischem Recht abgefasst und entsprechend auszulegen", True, "adjective + Recht"),
    ("Der Kaufvertrag unterliegt italienischem Recht.", True, "adjective + Recht"),
    ("Es gilt das Recht der Schweiz.", True, "Recht der <country>"),
    ("Es gilt das Recht der Bundesrepublik Deutschland.", False, "German law - not foreign"),
    ("Wir liefern auch in die Schweiz.", False, "country without 'Recht' - a destination"),
    ("Gerichtsstand ist Zürich.", False, "no choice of law at all"),
)


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n {title}\n{'=' * 78}")


def main() -> None:
    pd.set_option("display.width", 200)
    config = load_yaml(CONFIGS / "eval.yaml")
    scope_cfg, gate = config["scope"], config["gate"]

    head("INPUT - the remit, from configs/eval.yaml")
    print(f"  in-scope topics : {scope_cfg['topics']}")
    print(f"  gate            : {gate['scoped_clauses']} clauses / {gate['scoped_void']} void")

    head("PIECE 1 - the _FOREIGN_LAW pattern, case by case")
    for text, expected, why in REGEX_CASES:
        hit = bool(_FOREIGN_LAW.search(text))
        mark = "OK " if hit == expected else "BAD"
        print(f"  {mark} match={str(hit):5} ({why})\n      {text!r}")

    clauses = pd.read_parquet(INTERIM / "agb_clauses.parquet")

    head("PIECE 2 - foreign_law_contracts() runs on the FULL corpus, before narrowing")
    choice_of_law = clauses[clauses.topics_raw.apply(lambda t: APPLICABLE_LAW_TOPIC in t)]
    print(f"  clauses tagged {APPLICABLE_LAW_TOPIC!r} : {len(choice_of_law)}")
    print(f"  contracts having one             : {choice_of_law.contract_id.nunique()}")
    all_other = bool(choice_of_law.topics.apply(lambda t: t.tolist() == ["other"]).all())
    in_remit = int(
        choice_of_law.topics.apply(lambda t: {"liability", "warranty"} & set(t)).str.len().sum()
    )
    print("\n  every one of them maps to topics == ['other'], i.e. none is in the")
    print("  remit, so after the narrowing there would be nothing left to inspect:")
    print(f"    all map to ['other'] : {all_other}")
    print(f"    any in the remit     : {in_remit}")
    print("  -> this is why detection runs before step 3, not after")

    flagged = foreign_law_contracts(clauses)
    print(f"\n  contracts choosing NON-German law: {flagged}")
    for _, row in choice_of_law[choice_of_law.contract_id.isin(flagged)].iterrows():
        # Only some of a flagged shop's choice-of-law clauses are what matched;
        # the rest are printed for context and marked as such.
        matched = "MATCHED" if _FOREIGN_LAW.search(row.text) else "context"
        print(f"\n  --- contract {row.contract_id} ({row.clause_id}) [{matched}] ---")
        print(f"  {row.text[:300]}")

    head("COUNTERFACTUAL - what the spec's filter, read literally, would have cost")
    naive = set(choice_of_law.contract_id)
    scoped_preview = clauses[
        clauses.topics_raw.apply(lambda t: bool(set(scope_cfg["topics"]) & set(t)))
    ]
    would_lose = scoped_preview[scoped_preview.contract_id.isin(naive)]
    lost_contracts, all_contracts = (
        would_lose.contract_id.nunique(),
        scoped_preview.contract_id.nunique(),
    )
    lost_void, all_void = int(would_lose.label_void.sum()), int(scoped_preview.label_void.sum())
    print("  dropping every shop with an applicableLaw clause would remove")
    print(f"    {lost_contracts} of {all_contracts} contracts")
    print(f"    {len(would_lose)} of {len(scoped_preview)} clauses")
    print(f"    {lost_void} of {all_void} void clauses")
    print("  -> which is why the detector reads the text instead of the topic tag")

    head("FULL STEP - filter_scope()")
    result = filter_scope(INTERIM / "agb_clauses.parquet", scope_cfg["topics"], PROCESSED)
    print("\n  ScopeResult:")
    for field in ("clauses", "void", "contracts", "fragments", "foreign_law_clauses"):
        print(f"    {field:22} {getattr(result, field)}")
    print(f"    foreign_law_contracts  {result.foreign_law_contracts}")
    print(f"    path                   {result.path.relative_to(ROOT)}")

    scoped = pd.read_parquet(result.path)

    head("NARROWING - 3764 -> 523, and nothing lost inside the slice")
    print(f"  corpus in  : {len(clauses)}")
    print(f"  scoped out : {len(scoped)}")
    print(f"  expected   : {len(scoped_preview)}  (rows whose topics_raw meet the remit)")
    print(
        f"  identical  : {len(scoped) == len(scoped_preview)}  <- the filter removes nothing extra"
    )
    print(
        f"\n  new column : foreign_law_choice  {scoped.foreign_law_choice.value_counts().to_dict()}"
    )

    head("FLAGGED, NOT DROPPED")
    flagged_rows = scoped[scoped.foreign_law_choice]
    flagged_void = int(flagged_rows.label_void.sum())
    print(f"  foreign-law clauses kept : {len(flagged_rows)} (of which void: {flagged_void})")
    print(flagged_rows[["clause_id", "contract_id", "topics", "label_void"]].to_string(index=False))
    fragments = scoped[scoped.is_fragment]
    print(f"\n  fragments kept : {len(fragments)}")
    print(fragments[["clause_id", "contract_id", "text", "label_void"]].to_string(index=False))

    head("TOPIC MIX INSIDE SCOPE")
    counts: Counter = Counter()
    for raw in scoped.topics_raw:
        counts[", ".join(sorted(raw))] += 1
    for combo, count in counts.most_common():
        print(f"  {combo:38} {count:>4}")

    head("GATE CHECK (asserted by the flow in 2.7)")
    for name, got, want in (
        ("scoped_clauses", result.clauses, gate["scoped_clauses"]),
        ("scoped_void", result.void, gate["scoped_void"]),
    ):
        print(f"  {'PASS' if got == want else 'FAIL'}  {name:16} got {got:4}  want {want:4}")


if __name__ == "__main__":
    main()

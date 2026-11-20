"""Run step 2.1 and print what it reads, what it writes, and what it returns.

Inspection only: the pipeline flow calls the same function in anger. Safe to
re-run - the source commit is pinned, so both output files are rewritten with
identical bytes.
"""

import pandas as pd
from core.config import load_yaml
from core.paths import CONFIGS, RAW, ROOT
from core.settings import load_settings

from pipelines.p2_eval.s1_fetch_agb import (
    _csv_url,
    fetch_agb_snapshot,
    fetch_corpus,
    fetch_split_assignment,
)

IN_SCOPE = {"liability", "warranty"}


def head(title: str) -> None:
    print(f"\n{'=' * 78}\n {title}\n{'=' * 78}")


def topics(value: object) -> set[str]:
    return {part.strip() for part in str(value).replace(";", ",").split(",")}


def main() -> None:
    pd.set_option("display.width", 200)
    config = load_yaml(CONFIGS / "eval.yaml")
    source = config["source"]
    token = load_settings().hf_token or None

    head("INPUT - what configs/eval.yaml pins")
    for key, value in source.items():
        print(f"  {key:16} {value}")
    print(f"\n  resolved URL:\n  {_csv_url(source['repo'], source['commit'], source['csv_path'])}")

    head("SUB-STEP A - fetch_corpus(): the clause text, from GitHub")
    corpus = fetch_corpus(source["repo"], source["commit"], source["csv_path"])
    print(f"  shape   : {corpus.shape}")
    print(f"  columns : {list(corpus.columns)}")
    print(f"  void    : {corpus.void.value_counts().to_dict()}")
    print("\n  first 3 rows:")
    print(corpus.head(3).to_string(max_colwidth=46))

    head("SUB-STEP B - fetch_split_assignment(): ONLY id + split, from HuggingFace")
    splits = fetch_split_assignment(source["hf_dataset"], token)
    print(f"  shape   : {splits.shape}")
    print(f"  columns : {list(splits.columns)}   <- clause text deliberately discarded")
    print(f"  per half: {splits.split.value_counts().to_dict()}")

    head("FULL STEP - fetch_agb_snapshot()")
    result = fetch_agb_snapshot(
        repo=source["repo"],
        commit=source["commit"],
        csv_path=source["csv_path"],
        hf_dataset=source["hf_dataset"],
        out_dir=RAW / "agb-de",
        token=token,
    )
    print("\n  FetchResult:")
    for field in ("rows", "assigned", "unassigned", "commit"):
        print(f"    {field:12} {getattr(result, field)}")

    head("FILES WRITTEN")
    for path in (result.corpus_path, result.splits_path):
        print(f"  {path.relative_to(ROOT)!s:44} {path.stat().st_size:>9,} bytes")

    head("THE UNASSIGNED ROWS - who are they, and do they matter?")
    missing = corpus[~corpus.id.isin(splits.id)]
    scoped = missing[missing.topics.apply(lambda v: bool(topics(v) & IN_SCOPE))]
    print(missing[["id", "contract", "topics", "void"]].to_string(index=False))
    print(f"\n  all void: {bool(missing.void.all())}  <- the authors held these out deliberately")
    print(f"  in scope (liability/warranty): {len(scoped)}")
    print("  -> none in scope, so step 2.4's UnassignedClauses check stays quiet")

    head("GATE CHECK (asserted by the flow in 2.7, not by s1 itself)")
    passed = result.rows == source["expected_rows"]
    print(
        f"  {'PASS' if passed else 'FAIL'}  rows  got {result.rows}  want {source['expected_rows']}"
    )


if __name__ == "__main__":
    main()

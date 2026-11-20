"""Throwaway: profiles AGB-DE from both sources before any normalizer is written."""

import io
import sys
from collections import Counter

import pandas as pd
import requests
from core.logging import get_logger
from core.paths import REPORTS
from core.settings import load_settings

log = get_logger("spike.agbde")

HF_DATASET = "d4br4/agb-de"
# Pinned to the commit that added the corpus, so the snapshot is reproducible.
GH_COMMIT = "9b847807ec38809088e496c7847f94b4266ddf56"
GH_CSV = f"https://raw.githubusercontent.com/DaBr01/AGB-DE/{GH_COMMIT}/corpus/agb-de-anonym.csv"
GH_TREE = f"https://api.github.com/repos/DaBr01/AGB-DE/git/trees/{GH_COMMIT}?recursive=1"

IN_SCOPE = {"liability", "warranty"}
PLACEHOLDERS = ("<<NAME>>", "<<STADT>>", "<<STRASSE>>")
FRAGMENT_CHARS = 40


def repo_tree() -> list[str]:
    response = requests.get(GH_TREE, timeout=60)
    response.raise_for_status()
    return [item["path"] for item in response.json()["tree"] if item["type"] == "blob"]


def github_csv() -> pd.DataFrame:
    response = requests.get(GH_CSV, timeout=120)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text))


def hf_splits() -> dict[str, pd.DataFrame]:
    from datasets import get_dataset_config_names, load_dataset

    configs = get_dataset_config_names(HF_DATASET, token=load_settings().hf_token or None)
    log.info("hf configs: %s", configs)
    dataset = load_dataset(HF_DATASET, token=load_settings().hf_token or None)
    return {name: split.to_pandas() for name, split in dataset.items()}


def topic_counter(series: pd.Series) -> Counter:
    counts: Counter = Counter()
    for value in series.dropna():
        for part in str(value).replace(";", ",").split(","):
            part = part.strip()
            if part:
                counts[part] += 1
    return counts


def main() -> int:
    out: list[str] = ["# AGB-DE profile", ""]

    paths = repo_tree()
    out += ["## Repository files", "", *[f"- `{p}`" for p in sorted(paths)], ""]
    log.info("repo has %d files", len(paths))

    csv = github_csv()
    out += [
        "## GitHub corpus CSV",
        "",
        f"- source: `{GH_CSV}`",
        f"- rows: **{len(csv)}**, columns: **{len(csv.columns)}**",
        "",
        "| column | dtype | nulls | example |",
        "| --- | --- | --- | --- |",
    ]
    for column in csv.columns:
        sample = str(csv[column].dropna().iloc[0])[:70].replace("|", "\\|").replace("\n", " ")
        out.append(f"| `{column}` | {csv[column].dtype} | {csv[column].isna().sum()} | {sample} |")
    out.append("")

    log.info("csv columns: %s", list(csv.columns))
    log.info("csv rows: %d", len(csv))

    # void: how is it encoded, and what is the base rate
    void_counts = csv["void"].value_counts(dropna=False).to_dict() if "void" in csv else {}
    out += [f"- `void` value counts: `{void_counts}`", ""]

    topics = topic_counter(csv["topics"]) if "topics" in csv else Counter()
    subtopics = topic_counter(csv["subtopics"]) if "subtopics" in csv else Counter()
    out += [
        "## topics",
        "",
        *[f"- `{name}`: {count}" for name, count in topics.most_common()],
        "",
        f"## subtopics ({len(subtopics)} distinct)",
        "",
        *[f"- `{name}`: {count}" for name, count in subtopics.most_common()],
        "",
    ]
    log.info("distinct topics: %d, distinct subtopics: %d", len(topics), len(subtopics))

    # In-scope slice, which is what Phase 2 publishes
    if "topics" in csv:
        mask = (
            csv["topics"]
            .fillna("")
            .apply(
                lambda value: bool(
                    IN_SCOPE & {p.strip() for p in str(value).replace(";", ",").split(",")}
                )
            )
        )
        scoped = csv[mask]
        fragments = scoped["text"].fillna("").str.len() < FRAGMENT_CHARS
        out += [
            "## In-scope slice (liability or warranty)",
            "",
            f"- clauses: **{len(scoped)}**",
            f"- void: **{int(scoped['void'].sum())}**",
            f"- fragments under {FRAGMENT_CHARS} chars: {int(fragments.sum())}",
            f"- distinct contracts: "
            f"{scoped['contract'].nunique() if 'contract' in scoped else 'n/a'}",
            "",
        ]
        log.info("in scope: %d clauses, %d void", len(scoped), int(scoped["void"].sum()))

        # subtopics within scope decide whether gold labels can be auto-derived
        scoped_sub = topic_counter(scoped["subtopics"]) if "subtopics" in scoped else Counter()
        out += [
            f"### subtopics within scope ({len(scoped_sub)} distinct)",
            "",
            "| subtopic | clauses | void |",
            "| --- | --- | --- |",
        ]
        for name, count in scoped_sub.most_common():
            hit = scoped["subtopics"].fillna("").str.contains(name, regex=False)
            out.append(f"| `{name}` | {count} | {int(scoped.loc[hit, 'void'].sum())} |")
        out.append("")
        for name, count in scoped_sub.most_common(12):
            log.info("  subtopic %-38s %4d", name, count)

    placeholders = {
        token: int(csv["text"].fillna("").str.contains(token, regex=False).sum())
        for token in PLACEHOLDERS
    }
    out += [f"- anonymisation placeholders still present: `{placeholders}`", ""]

    splits = hf_splits()
    out += [
        "## HuggingFace splits",
        "",
        "| split | rows | columns | void |",
        "| --- | --- | --- | --- |",
    ]
    for name, frame in splits.items():
        label_col = next((c for c in ("label", "void") if c in frame.columns), None)
        void = int(frame[label_col].sum()) if label_col else -1
        out.append(f"| `{name}` | {len(frame)} | {', '.join(frame.columns)} | {void} |")
        log.info(
            "hf split %-6s rows=%-5d cols=%s void=%s", name, len(frame), list(frame.columns), void
        )
    out.append("")

    # Can the split be joined back to the annotated CSV?
    first = next(iter(splits.values()))
    joinable = [c for c in first.columns if c in csv.columns]
    out += [
        "## Joinability",
        "",
        f"- columns shared with the CSV: `{joinable}`",
    ]
    if "id" in joinable:
        ids = set(first["id"])
        out.append(f"- split ids found in CSV: {len(ids & set(csv['id']))} of {len(ids)}")
    out.append("")

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "p2_agbde_profile.md"
    path.write_text("\n".join(out), encoding="utf-8")
    log.info("profile written to %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

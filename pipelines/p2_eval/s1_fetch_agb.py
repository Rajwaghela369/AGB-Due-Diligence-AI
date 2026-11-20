"""2.1 Acquire: snapshot the AGB-DE corpus and the authors' split assignment."""

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests
from core.logging import get_logger

log = get_logger("p2.fetch")

# The split assignment is the only thing taken from the HuggingFace mirror; the
# clause text itself comes from the CSV, so there is one source of truth for it.
SPLIT_COLUMNS = ("id", "split")


@dataclass(frozen=True)
class FetchResult:
    corpus_path: Path
    splits_path: Path
    commit: str
    rows: int
    assigned: int
    unassigned: int


def _csv_url(repo: str, commit: str, csv_path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{commit}/{csv_path}"


def fetch_corpus(repo: str, commit: str, csv_path: str, timeout: int = 120) -> pd.DataFrame:
    """The corpus CSV at a pinned commit. Raw layer: rows and columns verbatim."""
    response = requests.get(_csv_url(repo, commit, csv_path), timeout=timeout)
    response.raise_for_status()
    return pd.read_csv(io.StringIO(response.text))


def fetch_split_assignment(hf_dataset: str, token: str | None = None) -> pd.DataFrame:
    """One row per clause id, naming the split the authors put it in.

    We train nothing. The split is reused so the development half and the
    held-out half stay the same ones the paper used.
    """
    from datasets import load_dataset

    dataset = load_dataset(hf_dataset, token=token)
    frames = [
        pd.DataFrame({"id": split.to_pandas()["id"], "split": name})
        for name, split in dataset.items()
    ]
    return pd.concat(frames, ignore_index=True)


def fetch_agb_snapshot(
    repo: str,
    commit: str,
    csv_path: str,
    hf_dataset: str,
    out_dir: Path,
    token: str | None = None,
) -> FetchResult:
    corpus = fetch_corpus(repo, commit, csv_path)
    splits = fetch_split_assignment(hf_dataset, token)

    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_file = out_dir / "agb-de-anonym.csv"
    splits_file = out_dir / f"splits_{commit[:8]}.parquet"

    corpus.to_csv(corpus_file, index=False)
    splits[list(SPLIT_COLUMNS)].to_parquet(splits_file, index=False)

    assigned = corpus["id"].isin(splits["id"]).sum()
    log.info(
        "snapshot %d clauses to %s, %d with a split assignment",
        len(corpus),
        corpus_file.name,
        assigned,
    )

    # The authors' splits cover slightly fewer rows than the corpus. Logged
    # rather than failed: step 2.4 decides what it means for the scoped slice,
    # where the gap turns out to be empty.
    unassigned = len(corpus) - int(assigned)
    if unassigned:
        log.warning("%d corpus rows sit in neither split", unassigned)

    return FetchResult(
        corpus_path=corpus_file,
        splits_path=splits_file,
        commit=commit,
        rows=len(corpus),
        assigned=int(assigned),
        unassigned=unassigned,
    )

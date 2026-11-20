"""Prepare the AVV gold set while keeping labels out of production stores.

The source is the Commission's official Article 28 standard-contractual-clause
template.  The checked-in annotations are human decisions; this module only
joins them to the pinned source text and prepares an explicitly approved
Langfuse upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import requests
from core import langfuse_client
from core.config import load_yaml
from core.paths import CONFIGS, GOLD, RAW
from lxml import etree

CELEX = "32021D0915"
CANONICAL_URL = f"https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{CELEX}"
CONTENT_URL = f"https://publications.europa.eu/resource/celex/{CELEX}"
REUSE_URL = "https://commission.europa.eu/legal-notice_en#copyright-notice"
RAW_FILENAME = "eu_scc_32021D0915_de.xhtml"
OBLIGATIONS = tuple("abcdefgh")
STATUSES = {"fulfilled", "failed", "silent"}
SPACE = re.compile(r"\s+")
Split = Literal["dev", "held_out"]


@dataclass(frozen=True)
class SourceClause:
    ordinal: int
    clause: str
    heading: str
    label: str | None
    text: str


def _normalise(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def fetch_official_template(out_dir: Path, *, session: Any = requests) -> Path:
    """Snapshot the official German Commission template and source metadata."""
    response = session.get(
        CONTENT_URL,
        headers={"Accept": "application/xhtml+xml", "Accept-Language": "de"},
        timeout=60,
    )
    response.raise_for_status()
    body = response.content
    root = etree.ElementTree(etree.fromstring(body))
    if not root.xpath("//*[local-name()='div' and @id='anx_1']"):
        raise ValueError("official template does not contain Annex I")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RAW_FILENAME
    path.write_bytes(body)
    manifest = {
        "celex": CELEX,
        "language": "de",
        "canonical_url": CANONICAL_URL,
        "content_url": CONTENT_URL,
        "retrieved_at": datetime.now(UTC).date().isoformat(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "licence": "European Commission reuse policy (Decision 2011/833/EU)",
        "licence_url": REUSE_URL,
    }
    (out_dir / "eu_scc_source.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def extract_source_clauses(path: Path) -> list[SourceClause]:
    """Return the template's numbered clause fragments in source order."""
    root = etree.parse(path)
    annex = root.xpath("//*[local-name()='div' and @id='anx_1']")
    if len(annex) != 1:
        raise ValueError(f"expected one Annex I, found {len(annex)}")

    current_clause = ""
    current_heading = ""
    extracted: list[SourceClause] = []
    for element in annex[0]:
        local_name = etree.QName(element).localname
        css_class = element.get("class", "")
        text = _normalise("".join(element.itertext()))
        if local_name == "p" and "oj-ti-grseq-1" in css_class:
            if text.startswith("Klausel"):
                current_clause = text
            elif current_clause:
                current_heading = text
            continue
        if not current_clause:
            continue

        label: str | None = None
        body = ""
        if local_name == "table":
            cells = element.xpath(".//*[local-name()='tr'][1]/*[local-name()='td']")
            if len(cells) >= 2:
                label = _normalise("".join(cells[0].itertext())).rstrip(".)")
                body = _normalise("".join(cells[1].itertext()))
        elif local_name == "p" and css_class == "oj-normal":
            body = text

        if body:
            extracted.append(
                SourceClause(
                    ordinal=len(extracted) + 1,
                    clause=current_clause,
                    heading=current_heading,
                    label=label,
                    text=body,
                )
            )
    return extracted


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _validate_labels(labels: dict[str, str], row_id: str) -> None:
    if tuple(labels) != OBLIGATIONS:
        raise ValueError(f"{row_id}: labels must be ordered a through h")
    unknown = set(labels.values()) - STATUSES
    if unknown:
        raise ValueError(f"{row_id}: unsupported statuses {sorted(unknown)}")


def build_gold_set(
    source_path: Path,
    annotations_path: Path,
    output_path: Path,
    source_manifest_path: Path,
) -> list[dict[str, Any]]:
    """Join source fragments and manual annotations into the auditable JSONL set."""
    source = {clause.ordinal: clause for clause in extract_source_clauses(source_path)}
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    annotations = _read_jsonl(annotations_path)
    rows: list[dict[str, Any]] = []
    for annotation in annotations:
        row_id = annotation["id"]
        labels = annotation["labels"]
        _validate_labels(labels, row_id)
        selected = source.get(annotation["source_ordinal"])
        if selected is None:
            raise ValueError(f"{row_id}: source ordinal does not exist")
        is_adapted = "adapted_text" in annotation
        text = annotation.get("adapted_text", selected.text)
        citation = " — ".join(
            part
            for part in (
                selected.clause,
                selected.heading,
                f"lit. {selected.label}" if selected.label else None,
            )
            if part
        )
        rows.append(
            {
                "id": row_id,
                "split": annotation["split"],
                "title": citation,
                "text": text,
                "labels": labels,
                "fulfilled": [code for code, status in labels.items() if status == "fulfilled"],
                "failed": [code for code, status in labels.items() if status == "failed"],
                "silent": [code for code, status in labels.items() if status == "silent"],
                "label_notes": annotation.get("label_notes", {}),
                "source": {
                    "name": "EU-Standardvertragsklauseln Verantwortlicher–Auftragsverarbeiter",
                    "celex": CELEX,
                    "url": CANONICAL_URL,
                    "citation": citation,
                    "source_ordinal": selected.ordinal,
                    "verbatim": not is_adapted,
                    "adaptation": annotation.get("adaptation"),
                    "retrieved_at": source_manifest["retrieved_at"],
                    "licence": source_manifest["licence"],
                    "licence_url": source_manifest["licence_url"],
                },
            }
        )

    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("AVV gold ids are not unique")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return rows


def build_dataset_items(rows: list[dict[str, Any]], split: Split = "held_out") -> list[dict]:
    """Build the Langfuse answer key; labels remain in expected output only."""
    return [
        {
            "id": row["id"],
            "input": {"clause_id": row["id"], "title": row["title"], "text": row["text"]},
            "expected_output": {
                "obligations": row["labels"],
                "fulfilled": row["fulfilled"],
                "failed": row["failed"],
            },
            "metadata": {
                "split": row["split"],
                "source_celex": row["source"]["celex"],
                "source_citation": row["source"]["citation"],
                "verbatim": row["source"]["verbatim"],
            },
        }
        for row in rows
        if row["split"] == split
    ]


def upload_dataset(items: list[dict], name: str, *, approved: bool) -> int:
    """Upload the held-out key only when a human approved the external write."""
    if not approved:
        raise PermissionError("Langfuse upload requires --approve-live-upload")
    client = langfuse_client.client()
    client.create_dataset(
        name=name,
        description=(
            "Hand-labelled AVV clauses for Art. 28(3)(a)-(h), held-out split; "
            "includes clearly marked counterfactual negative variants."
        ),
    )
    for item in items:
        client.create_dataset_item(dataset_name=name, **item)
    client.flush()
    return len(items)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="refresh the official template")
    parser.add_argument("--upload", action="store_true", help="upload the held-out split")
    parser.add_argument(
        "--approve-live-upload",
        action="store_true",
        help="confirm that the orchestrator approved the Langfuse write",
    )
    args = parser.parse_args()

    raw_dir = RAW / "dsgvo_avv"
    if args.fetch:
        fetch_official_template(raw_dir)
    gold_dir = GOLD / "avv"
    rows = build_gold_set(
        raw_dir / RAW_FILENAME,
        gold_dir / "annotations.jsonl",
        gold_dir / "avv_clauses.jsonl",
        raw_dir / "eu_scc_source.json",
    )
    if args.upload:
        config = load_yaml(CONFIGS / "eval.yaml")["avv"]
        items = build_dataset_items(rows, split=config["splits"]["held_out"])
        upload_dataset(items, config["publish"]["dataset"], approved=args.approve_live_upload)


if __name__ == "__main__":
    main()

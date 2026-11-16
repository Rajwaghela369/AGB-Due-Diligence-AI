"""Measure exact splitter boundaries against the draft public-document gold set."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.ingest.parse import parse_pdf
from app.ingest.split import split, words_from_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = ROOT / "data" / "gold" / "splitter"
WORD_SAMPLE = 6


def _boundary(clause) -> dict[str, str | None]:
    words = clause.text.split()
    return {
        "number": clause.number,
        "first_words": " ".join(words[:WORD_SAMPLE]),
        "last_words": " ".join(words[-WORD_SAMPLE:]),
    }


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(f"{namespace}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{namespace}t":
                parts.append(node.text or "")
            elif node.tag == f"{namespace}tab":
                parts.append(" ")
            elif node.tag == f"{namespace}br":
                parts.append("\n")
        paragraphs.extend(line.strip() for line in "".join(parts).splitlines() if line.strip())
    return "\n".join(paragraphs)


def _actual(source: Path):
    if source.suffix.casefold() == ".pdf":
        words, _ = parse_pdf(source.read_bytes())
    elif source.suffix.casefold() == ".docx":
        words = words_from_text(_docx_text(source))
    else:
        raise ValueError(f"unsupported gold source: {source}")
    return [_boundary(clause) for clause in split(words)]


def _key(boundary: dict) -> tuple[str, str, str]:
    def clean(value: str | None) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    return (
        clean(boundary.get("number")),
        clean(boundary["first_words"]),
        clean(boundary["last_words"]),
    )


def evaluate(directory: Path) -> int:
    matched_total = expected_total = actual_total = 0
    manifests = sorted(directory.glob("*.json"))
    if not manifests:
        raise SystemExit(f"no gold manifests in {directory}")

    for manifest in manifests:
        gold = json.loads(manifest.read_text(encoding="utf-8"))
        source_name = gold.get("file") or manifest.with_suffix("").name
        source = directory / source_name
        expected = gold["clauses"]
        actual = _actual(source)
        expected_keys = {_key(boundary) for boundary in expected}
        actual_keys = {_key(boundary) for boundary in actual}
        matched = len(expected_keys & actual_keys)
        score = matched / len(expected_keys) if expected_keys else 1.0
        print(
            f"{source.name}: {matched}/{len(expected_keys)} exact "
            f"({score:.1%}); actual={len(actual_keys)}"
        )
        matched_total += matched
        expected_total += len(expected_keys)
        actual_total += len(actual_keys)

    score = matched_total / expected_total if expected_total else 1.0
    print(f"overall: {matched_total}/{expected_total} exact ({score:.1%}); actual={actual_total}")
    return 0 if score >= 0.95 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", type=Path, default=DEFAULT_GOLD)
    return evaluate(parser.parse_args().directory)


if __name__ == "__main__":
    raise SystemExit(main())

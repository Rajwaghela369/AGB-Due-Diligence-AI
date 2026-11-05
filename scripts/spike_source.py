"""Throwaway: answers the three source questions, then gets deleted (task W2)."""

import argparse
import re
import sys
from typing import Any

from core.logging import get_logger
from core.paths import REPORTS
from core.settings import load_settings

log = get_logger("spike")

DATASET = "openlegaldata/laws-germany"
DEFAULT_CONFIG = "dump-20260520"

# The three paragraphs that stress every assumption in the chunker design:
# 309 is a 13-item list with lettered sub-items, 307 is prose where the Satz
# carries legal weight, 434 tells us whether the text predates the 2022 reform.
TARGETS = {"309", "307", "434"}


def pinnable_revision(token: str | None) -> str | None:
    from huggingface_hub import HfApi

    return HfApi(token=token).dataset_info(DATASET).sha


def available_configs(token: str | None) -> list[str]:
    from datasets import get_dataset_config_names

    return get_dataset_config_names(DATASET, token=token)


def scan(
    config: str, limit: int | None, token: str | None
) -> tuple[dict[str, dict[str, Any]], int, list[str]]:
    """Stream until every target BGB section is found, or the limit is reached."""
    from datasets import load_dataset

    ds = load_dataset(DATASET, config, split="train", streaming=True, token=token)
    found: dict[str, dict[str, Any]] = {}
    fields: list[str] = []
    scanned = 0

    for scanned, record in enumerate(ds, start=1):  # noqa: B007 - count is reported
        if not fields:
            fields = sorted(record.keys())

        if record.get("book_code") == "BGB" and record.get("section") in TARGETS:
            found[record["section"]] = record
            log.info("found BGB § %s after %d rows", record["section"], scanned)
            if TARGETS <= found.keys():
                return found, scanned, fields

        if scanned % 20_000 == 0:
            log.info("scanned %d rows, found %d/%d targets", scanned, len(found), len(TARGETS))

        if limit and scanned >= limit:
            return found, scanned, fields

    return found, scanned, fields


def diagnose(config: str, token: str | None) -> None:
    """Why did the book_code/section filter match nothing? Print the real values."""
    from collections import Counter

    from datasets import load_dataset

    ds = load_dataset(DATASET, config, split="train", streaming=True, token=token)

    books: Counter[str] = Counter()
    slugs: Counter[str] = Counter()
    samples: list[tuple[Any, Any, Any]] = []
    types_seen: set[str] = set()

    for scanned, record in enumerate(ds, start=1):
        books[repr(record.get("book_code"))] += 1
        slugs[repr(record.get("book_slug"))] += 1
        types_seen.add(type(record.get("section")).__name__)

        # Anything that smells like the BGB, however it is spelled.
        code = str(record.get("book_code") or "")
        slug = str(record.get("book_slug") or "")
        if len(samples) < 12 and "bgb" in f"{code}{slug}".lower():
            samples.append((record.get("book_code"), record.get("section"), record.get("title")))

        if scanned % 20_000 == 0:
            log.info("diagnosed %d rows", scanned)

    log.info("section field types: %s", ", ".join(sorted(types_seen)))
    log.info("top 25 book_code values:")
    for value, count in books.most_common(25):
        log.info("  %-28s %6d", value, count)
    log.info("top 10 book_slug values:")
    for value, count in slugs.most_common(10):
        log.info("  %-28s %6d", value, count)

    if samples:
        log.info("rows that look like BGB:")
        for code, section, title in samples:
            log.info("  book_code=%r section=%r title=%r", code, section, title)
    else:
        log.warning("no row anywhere mentions bgb in book_code or book_slug")


def structure_report(html: str) -> dict[str, Any]:
    """Which markup the item-level chunker can rely on."""
    tags = re.findall(r"<\s*([A-Za-z]+)", html or "")
    counts: dict[str, int] = {}
    for tag in tags:
        counts[tag.upper()] = counts.get(tag.upper(), 0) + 1
    return {
        "tag_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        "has_definition_list": {"DL", "DT", "DD"} <= counts.keys(),
        "has_paragraphs": "P" in counts,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="dataset config; the -1k and -10k variants are small samples",
    )
    ap.add_argument(
        "--diagnose",
        action="store_true",
        help="print the real book_code/section values instead of scanning for targets",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N streamed rows; use a small value for a plumbing test",
    )
    args = ap.parse_args()

    # An empty HF_TOKEN means anonymous; the token is never exported to the
    # environment, so it has to be handed to both HF clients explicitly.
    token = load_settings().hf_token or None
    auth_mode = "token from .env" if token else "anonymous"

    try:
        revision = pinnable_revision(token)
        configs = available_configs(token)
        log.info("dataset reachable (%s), revision %s", auth_mode, revision)
        log.info("available configs: %s", ", ".join(configs))
    except Exception as exc:  # noqa: BLE001 - a gated dataset is a finding, not a crash
        log.error("could not reach %s (%s): %s", DATASET, auth_mode, exc)
        log.error("if this is an auth error, set HF_TOKEN in .env and retry")
        return 1

    if args.diagnose:
        diagnose(args.config, token)
        return 0

    if args.config not in configs:
        log.error("unknown config %r; available: %s", args.config, ", ".join(configs))
        return 1

    found, scanned, fields = scan(args.config, args.limit, token)
    missing = sorted(TARGETS - found.keys())

    lines = [
        "# P1 spike findings",
        "",
        f"- dataset: `{DATASET}`",
        f"- config: `{args.config}`",
        f"- pinnable revision: `{revision}`",
        f"- available configs: {', '.join(f'`{c}`' for c in configs)}",
        f"- access: {auth_mode}",
        f"- rows streamed: {scanned}" + (f" (limit {args.limit})" if args.limit else ""),
        f"- record fields: {', '.join(fields)}",
        "",
    ]

    if missing:
        lines += [f"> Sections not reached within the scan: {', '.join(missing)}", ""]
        log.warning("not found within %d rows: %s", scanned, ", ".join(missing))

    for section in sorted(found):
        record = found[section]
        html = record.get("content") or ""
        structure = structure_report(html)
        lines += [
            f"## § {section} — {record.get('title')}",
            "",
            f"- doknr: `{record.get('doknr')}`",
            f"- updated_date: `{record.get('updated_date')}`",
            f"- tags in `content`: `{structure['tag_counts']}`",
            f"- **item markup (DL/DT/DD) present: {structure['has_definition_list']}**",
            "",
            "### raw `content` (first 2500 chars)",
            "",
            "```html",
            html[:2500],
            "```",
            "",
            "### `markdown_content` (first 1200 chars)",
            "",
            "```text",
            (record.get("markdown_content") or "")[:1200],
            "```",
            "",
        ]

    REPORTS.mkdir(parents=True, exist_ok=True)
    out = REPORTS / "p1_spike.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("findings written to %s", out)

    for section in sorted(found):
        structure = structure_report(found[section].get("content") or "")
        log.info("§ %-4s item markup: %s", section, structure["has_definition_list"])

    return 0 if not missing else 2


if __name__ == "__main__":
    sys.exit(main())

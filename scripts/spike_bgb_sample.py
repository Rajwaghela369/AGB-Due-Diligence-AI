"""Throwaway: profiles every BGB row so the chunker is designed on facts, not guesses."""

import re
import sys
from collections import Counter
from typing import Any

from core.config import load_yaml
from core.logging import get_logger
from core.paths import CONFIGS, REPORTS
from core.settings import load_settings

log = get_logger("bgb-sample")

DATASET = "openlegaldata/laws-germany"
CONFIG = "dump-20260520"

# Prose, 13-item list, and the paragraph that reveals whether the text is
# post-2022 Kaufrecht reform.
SHOWCASE = ("§ 307", "§ 308", "§ 309", "§ 434")

SECTION_SHAPES = {
    "§ N": re.compile(r"^§ \d+$"),
    "§ Na": re.compile(r"^§ \d+[a-z]$"),
    "§ N a-z multi": re.compile(r"^§ \d+[a-z]{2,}$"),
    "Anlage/other": re.compile(r"^(?!§ \d)"),
}


def fetch_bgb(token: str | None) -> list[dict[str, Any]]:
    from datasets import load_dataset

    ds = load_dataset(DATASET, CONFIG, split="train", streaming=True, token=token)
    rows = []
    for scanned, record in enumerate(ds, start=1):
        if record.get("book_code") == "BGB":
            rows.append(record)
        if scanned % 40_000 == 0:
            log.info("scanned %d rows, kept %d", scanned, len(rows))
    return rows


def classify_sections(rows: list[dict[str, Any]]) -> tuple[Counter[str], list[str]]:
    shapes: Counter[str] = Counter()
    oddities = []
    for row in rows:
        section = row.get("section") or ""
        for label, pattern in SECTION_SHAPES.items():
            if pattern.match(section):
                shapes[label] += 1
                if label == "Anlage/other":
                    oddities.append(section)
                break
        else:
            shapes["unmatched"] += 1
            oddities.append(section)
    return shapes, oddities[:20]


def tag_profile(rows: list[dict[str, Any]]) -> tuple[Counter[str], int, int]:
    """Which markup exists across the whole book, and how many sections carry lists."""
    tags: Counter[str] = Counter()
    with_list = 0
    with_paragraph = 0
    for row in rows:
        found = {t.upper() for t in re.findall(r"<\s*([A-Za-z]+)", row.get("content") or "")}
        tags.update(found)
        if {"DL", "DT", "DD"} <= found:
            with_list += 1
        if "P" in found:
            with_paragraph += 1
    return tags, with_list, with_paragraph


def extract_items(html: str) -> list[tuple[str, str]]:
    """Pull DT/DD pairs the way the real chunker will, to prove item splitting works."""
    from lxml import html as lxml_html

    tree = lxml_html.fragment_fromstring(html, create_parent="div")
    items = []
    for dl in tree.iter("dl"):
        terms = dl.findall("dt")
        definitions = dl.findall("dd")
        for dt, dd in zip(terms, definitions, strict=False):
            label = (dt.text_content() or "").strip()
            body = " ".join((dd.text_content() or "").split())
            items.append((label, body))
    return items


def check_whitelist(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Do the refs in kb_paragraphs.yaml match the dump's section values verbatim?"""
    whitelist = [p["ref"] for p in load_yaml(CONFIGS / "kb_paragraphs.yaml")["paragraphs"]]
    present = {row.get("section") for row in rows}
    return [r for r in whitelist if r in present], [r for r in whitelist if r not in present]


def main() -> int:
    token = load_settings().hf_token or None
    rows = fetch_bgb(token)
    log.info("BGB rows: %d", len(rows))

    sections = [r.get("section") or "" for r in rows]
    duplicates = [s for s, n in Counter(sections).items() if n > 1]
    shapes, oddities = classify_sections(rows)
    tags, with_list, with_paragraph = tag_profile(rows)
    resolved, missing = check_whitelist(rows)
    by_section = {r.get("section"): r for r in rows}

    out = [
        "# BGB structure profile",
        "",
        f"- rows with `book_code == 'BGB'`: **{len(rows)}**",
        f"- unique `section` values: {len(set(sections))}",
        f"- duplicate sections: {duplicates or 'none'}",
        "",
        "## `section` value shapes",
        "",
        *[f"- `{label}`: {count}" for label, count in shapes.most_common()],
        "",
        f"Non-paragraph sections (first 20): {oddities or 'none'}",
        "",
        "## Markup across all BGB sections",
        "",
        f"- sections containing a DL/DT/DD list: **{with_list}** of {len(rows)}",
        f"- sections containing <P>: **{with_paragraph}** of {len(rows)}",
        "",
        *[f"- `<{tag}>` appears in {count} sections" for tag, count in tags.most_common(15)],
        "",
        "## Whitelist resolution",
        "",
        f"- resolved: **{len(resolved)}** of {len(resolved) + len(missing)}",
        f"- missing: {missing or 'none'}",
        "",
    ]

    for section in SHOWCASE:
        row = by_section.get(section)
        if row is None:
            out += [f"## {section} — NOT FOUND", ""]
            continue

        items = extract_items(row.get("content") or "")
        out += [
            f"## {section} — {row.get('title')}",
            "",
            f"- doknr: `{row.get('doknr')}` · updated_date: `{row.get('updated_date')}`",
            f"- DT/DD items extracted: **{len(items)}**",
            "",
        ]
        if items:
            out += ["### extracted items", ""]
            out += [f"- `{label}` → {body[:220]}" for label, body in items]
            out += [""]
        out += [
            "### raw `content`",
            "",
            "```html",
            row.get("content") or "",
            "```",
            "",
            "### `markdown_content`",
            "",
            "```text",
            row.get("markdown_content") or "",
            "```",
            "",
        ]

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / "p1_bgb_profile.md"
    path.write_text("\n".join(out), encoding="utf-8")

    log.info("sections with DL/DT/DD lists: %d of %d", with_list, len(rows))
    log.info("whitelist resolved: %d, missing: %s", len(resolved), missing or "none")
    for section in SHOWCASE:
        row = by_section.get(section)
        count = len(extract_items(row.get("content") or "")) if row else -1
        log.info("%-8s items extracted: %s", section, count if count >= 0 else "NOT FOUND")
    log.info("profile written to %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

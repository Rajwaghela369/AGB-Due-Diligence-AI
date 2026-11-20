"""1.3 Chunk: turn the content HTML of each norm into retrievable units."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging import get_logger
from core.schemas import LawChunk
from lxml import etree

log = get_logger("p1.chunk")

LAW = "BGB"
SOURCE_URL = "https://www.gesetze-im-internet.de/bgb/__{number}.html"

# Absatz markers open a <P>: "(1) Die Sache ist frei von ...".
ABSATZ = re.compile(r"^\((\d+)\)\s*")

# Official sub-headings open a <DD> in parentheses and never start with a digit,
# which is what separates them from an Absatz marker.
HEADING = re.compile(r"^\((?!\d)([^)]{3,})\)\s*")

# The content is XML-derived markup with uppercase tags, so it is parsed as XML.
# lxml's HTML parser would hoist each <DL> out of its <P>, because HTML forbids
# block elements inside a paragraph, and the lists would be lost.
PARAGRAPH_TAG = "P"
LIST_TAG = "DL"
TERM_TAG = "DT"
DEFINITION_TAG = "DD"


def _norm(text: str) -> str:
    return " ".join((text or "").split())


def _text(element, skip_lists: bool = False) -> str:
    """Element text with explicit separators, optionally skipping nested lists.

    lxml concatenates without whitespace, which otherwise glues a sub-heading to
    the sentence that follows it.
    """
    parts = [element.text or ""]
    for child in element:
        if skip_lists and child.tag == LIST_TAG:
            parts.append(child.tail or "")  # text after a list still belongs here
            continue
        parts.append(_text(child, skip_lists))
        parts.append(child.tail or "")
    return " ".join(part for part in parts if part)


def _partition(element, stops: set, state: dict) -> tuple[list[str], list[str]]:
    """Collect text in document order, flipping once a stop element is passed."""
    before: list[str] = []
    after: list[str] = []

    def push(text: str | None) -> None:
        if text:
            (after if state["seen"] else before).append(text)

    push(element.text)
    for child in element:
        if child in stops:
            state["seen"] = True
        else:
            child_before, child_after = _partition(child, stops, state)
            before += child_before
            after += child_after
        push(child.tail)

    return before, after


def _around_lists(container) -> tuple[str, str, list]:
    """Text before and after the shallowest nested lists, plus those lists.

    Position matters: a <DD> can hold its lead-in and a closing qualifier in
    separate <LA> siblings around the list, so the split cannot work off direct
    children alone.
    """
    lists = _shallowest_lists(container)
    before, after = _partition(container, set(lists), {"seen": False})
    return _norm(" ".join(before)), _norm(" ".join(after)), lists


def _shallowest_lists(container) -> list:
    """Only the first level of nesting; deeper lists stay inline in their parent."""
    found = container.xpath(f".//{LIST_TAG}")
    if not found:
        return []

    def depth(element) -> int:
        steps = 0
        while element is not container:
            element = element.getparent()
            steps += 1
        return steps

    minimum = min(depth(element) for element in found)
    return [element for element in found if depth(element) == minimum]


def _pairs(dl) -> list[tuple[str, Any]]:
    """DT/DD pairs with the label cleaned: '7.' -> '7', 'a)' -> 'a'."""
    terms = [child for child in dl if child.tag == TERM_TAG]
    definitions = [child for child in dl if child.tag == DEFINITION_TAG]
    return [
        (_norm("".join(dt.itertext())).rstrip(".)"), dd)
        for dt, dd in zip(terms, definitions, strict=False)
    ]


def _heading_and_body(text: str) -> tuple[str | None, str]:
    match = HEADING.match(text)
    if not match:
        return None, text
    return _norm(match.group(1)), text[match.end() :].strip()


def _section_number(section: str) -> str:
    return section.replace("§", "").strip()


def _embed_text(citation: str, paragraph_title: str, chunk_title: str, text: str) -> str:
    """Law, citation and both titles prefix the text, so a fragment still embeds in context."""
    title = (
        f"{paragraph_title} — {chunk_title}" if chunk_title != paragraph_title else paragraph_title
    )
    return f"{LAW} {citation} {title}: {text}"


def _join(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _chunk(
    record: dict[str, Any],
    chunk_id: str,
    citation: str,
    title: str,
    item: str | None,
    sub_item: str | None,
    text: str,
) -> LawChunk:
    number = _section_number(record["section"])
    return LawChunk(
        chunk_id=chunk_id,
        law=LAW,
        paragraph=record["section"],
        item=item,
        sub_item=sub_item,
        title=title,
        citation=citation,
        topics=_topics(record, item),
        embed_text=_embed_text(citation, record["title"], title, text),
        text=text,
        source_dump=record["source"].get("config", ""),
        source_url=SOURCE_URL.format(number=number.lower()),
    )


@dataclass(frozen=True)
class ChunkResult:
    path: Path
    report_path: Path
    chunks: list[LawChunk]


def _topics(record: dict[str, Any], item: str | None) -> list[str]:
    if item is None:
        return record["topics"]
    return record.get("item_topics", {}).get(item, record["topics"])


def _absatz_chunks(record: dict[str, Any], paragraphs: list) -> list[LawChunk]:
    """One chunk per Absatz. An unnumbered <P> continues the previous one."""
    section = record["section"]
    number = _section_number(section)
    chunks: list[LawChunk] = []

    for paragraph in paragraphs:
        text = _norm(_text(paragraph))
        if not text:
            continue

        match = ABSATZ.match(text)
        if match is None:
            if chunks:
                previous = chunks[-1]
                chunks[-1] = previous.model_copy(
                    update={
                        "text": f"{previous.text} {text}",
                        "embed_text": f"{previous.embed_text} {text}",
                    }
                )
                continue
            # A single unnumbered paragraph is the whole norm.
            citation = section
            chunk_id = f"bgb-{number}"
            body = text
        else:
            absatz = match.group(1)
            citation = f"{section} Abs. {absatz}"
            chunk_id = f"bgb-{number}-abs{absatz}"
            body = text[match.end() :].strip()

        chunks.append(
            _chunk(
                record,
                chunk_id=chunk_id,
                citation=citation,
                title=record["title"],
                item=None,
                sub_item=None,
                text=body,
            )
        )

    return chunks


def _item_chunks(record: dict[str, Any], paragraphs: list) -> list[LawChunk]:
    """One chunk per numbered item, carrying the paragraph's Einleitungssatz.

    Items listed in `split_subitems` are emitted as their lettered sub-items
    instead, because those are separate legal grounds rather than instances of
    one rule.
    """
    section = record["section"]
    number = _section_number(section)
    split = set(record.get("split_subitems", []))
    chunks: list[LawChunk] = []

    for paragraph in paragraphs:
        stem, tail, lists = _around_lists(paragraph)

        for dl in lists:
            for item, dd in _pairs(dl):
                if item in split:
                    lead, qualifier, sub_lists = _around_lists(dd)
                else:
                    lead, qualifier, sub_lists = _norm(_text(dd)), "", []

                heading, lead_body = _heading_and_body(lead)
                item_title = heading or record["title"]

                if not sub_lists:
                    citation = f"{section} Nr. {item}"
                    text = _join(stem, lead_body, qualifier, tail)
                    chunks.append(
                        _chunk(
                            record,
                            chunk_id=f"bgb-{number}-{item}",
                            citation=citation,
                            title=item_title,
                            item=item,
                            sub_item=None,
                            text=text,
                        )
                    )
                    continue

                for sub_dl in sub_lists:
                    for sub_item, sub_dd in _pairs(sub_dl):
                        sub_heading, sub_body = _heading_and_body(_norm(_text(sub_dd)))
                        sub_title = (
                            " — ".join(part for part in (heading, sub_heading) if part)
                            or record["title"]
                        )
                        # The item's lead-in precedes its sub-items; its closing
                        # qualifier follows them.
                        text = _join(stem, lead_body, sub_body, qualifier, tail)
                        chunks.append(
                            _chunk(
                                record,
                                chunk_id=f"bgb-{number}-{item}-{sub_item}",
                                citation=f"{section} Nr. {item} lit. {sub_item}",
                                title=sub_title,
                                item=item,
                                sub_item=sub_item,
                                text=text,
                            )
                        )

    return chunks


def chunk_record(record: dict[str, Any]) -> list[LawChunk]:
    tree = etree.fromstring(f"<root>{record['content']}</root>")
    paragraphs = [child for child in tree if child.tag == PARAGRAPH_TAG] or [tree]

    if record["level"] == "item":
        return _item_chunks(record, paragraphs)
    return _absatz_chunks(record, paragraphs)


def chunk_norms(selected: Path, out_dir: Path, report_dir: Path) -> ChunkResult:
    records = [json.loads(line) for line in selected.read_text(encoding="utf-8").splitlines()]

    chunks: list[LawChunk] = []
    for record in records:
        produced = chunk_record(record)
        if not produced:
            raise ValueError(f"{record['section']} produced no chunks")
        chunks.extend(produced)

    ids = [chunk.chunk_id for chunk in chunks]
    duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
    if duplicates:
        raise ValueError(f"duplicate chunk ids: {', '.join(duplicates)}")

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "bgb_chunks.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for chunk in chunks:
            fh.write(chunk.model_dump_json() + "\n")

    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "p1_chunks.md"
    report_path.write_text(render_report(chunks), encoding="utf-8")

    log.info("produced %d chunks from %d norms", len(chunks), len(records))
    return ChunkResult(path=path, report_path=report_path, chunks=chunks)


def render_report(chunks: list[LawChunk]) -> str:
    """Every chunk in reading order, for the manual review in task 1.3g."""
    lines = [
        "# BGB chunks",
        "",
        f"{len(chunks)} chunks. Check: is the stem present, are items split correctly, "
        "is any text truncated or duplicated?",
        "",
    ]
    for chunk in chunks:
        lines += [
            f"## `{chunk.chunk_id}` — {chunk.citation}",
            "",
            f"- title: {chunk.title}",
            f"- topics: {', '.join(chunk.topics)}",
            f"- chars: {len(chunk.text)}",
            "",
            chunk.text,
            "",
        ]
    return "\n".join(lines)

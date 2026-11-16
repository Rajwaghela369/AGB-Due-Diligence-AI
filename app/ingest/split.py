"""Split German AGB at the smallest explicit unit that contains prose.

Section and heading-only lines establish context but are not emitted as empty clauses.
Once a numbered unit contains text, that unit is the clause boundary; child markers such
as ``(1)`` or ``a)`` therefore win over their enclosing ``§ 6``/``6.1`` heading. This is
the lowest stable granularity that normally carries a complete sentence and gives the UI
useful highlight regions without splitting ordinary wrapped sentences.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from core.hashing import content_hash

from app.domain import ClauseSpan, Rect

_SECTION = re.compile(r"^(§\s*\d+[a-z]?)\s*(.*)$", re.IGNORECASE)
_DECIMAL = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){0,3}\.?)\s+(.+)$")
_PAREN = re.compile(r"^(\(\d+\))\s*(.*)$")
_LETTER = re.compile(r"^([a-z]\))\s*(.*)$", re.IGNORECASE)
_NUMBER_PAREN = re.compile(r"^(\d+\))\s*(.*)$")
_PAGE_NUMBER = re.compile(r"^(?:seite\s+)?\d+(?:\s+(?:von|/)\s+\d+)?$", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?;:]$|[.!?;:][\"'»)]$")


@dataclass
class _Line:
    text: str
    page: int | None
    x0: float | None = None
    top: float | None = None
    x1: float | None = None
    bottom: float | None = None
    page_height: float | None = None
    start: int | None = None
    end: int | None = None

    def rect(self) -> Rect | None:
        if self.page is None or None in (self.x0, self.top, self.x1, self.bottom):
            return None
        return Rect(page=self.page, x0=self.x0, top=self.top, x1=self.x1, bottom=self.bottom)


@dataclass
class _Pending:
    number: str | None
    heading: str | None
    lines: list[_Line] = field(default_factory=list)


def words_from_text(text: str) -> list[dict[str, Any]]:
    """Represent pasted text as synthetic positioned words with source offsets."""
    words: list[dict[str, Any]] = []
    for line_number, match in enumerate(re.finditer(r"[^\r\n]+", text)):
        for token in re.finditer(r"\S+", match.group()):
            start = match.start() + token.start()
            end = match.start() + token.end()
            words.append(
                {
                    "text": token.group(),
                    "page": None,
                    "line": line_number,
                    "start": start,
                    "end": end,
                }
            )
    return words


def split(words: list[dict[str, Any]]) -> list[ClauseSpan]:
    """Purely turn parsed or synthetic words into ordered clause spans."""
    lines = _drop_running_matter(_lines(words))
    clauses: list[ClauseSpan] = []
    pending: _Pending | None = None
    heading: str | None = None

    def flush() -> None:
        nonlocal pending
        if pending is None or not pending.lines:
            pending = None
            return
        text = _join(pending.lines)
        if not text:
            pending = None
            return
        rects = [rect for line in pending.lines if (rect := line.rect()) is not None]
        starts = [line.start for line in pending.lines if line.start is not None]
        ends = [line.end for line in pending.lines if line.end is not None]
        clauses.append(
            ClauseSpan(
                position=len(clauses),
                number=pending.number,
                heading=pending.heading,
                text=text,
                content_hash=content_hash(text),
                page=rects[0].page if rects else None,
                rects=rects,
                start=min(starts) if starts else None,
                end=max(ends) if ends else None,
                in_scope=True,
            )
        )
        pending = None

    for line in lines:
        marker = _marker(line.text)
        if marker is not None:
            number, remainder, is_section = marker
            if _heading_only(remainder):
                flush()
                if not remainder and not is_section and number.endswith(")"):
                    pending = _Pending(number=number, heading=heading, lines=[line])
                    continue
                heading = f"{number} {remainder}".strip() if remainder else number
                pending = None
                continue
            flush()
            if is_section:
                heading = f"{number} {remainder.split('.', 1)[0]}".strip()
            pending = _Pending(number=number, heading=heading, lines=[line])
            continue

        if _standalone_heading(line.text):
            flush()
            heading = line.text
            continue
        if pending is None:
            pending = _Pending(number=None, heading=heading)
        pending.lines.append(line)

    flush()
    return clauses


def _lines(words: list[dict[str, Any]]) -> list[_Line]:
    grouped: dict[tuple[int, int | None, int], list[dict[str, Any]]] = defaultdict(list)
    for order, word in enumerate(words):
        page = word.get("page")
        line = word.get("line")
        if line is None:
            line = round(float(word.get("top", order)) / 3)
        grouped[(page if page is not None else -1, page, int(line))].append(word)

    by_page: dict[int | None, list[_Line]] = defaultdict(list)
    for (_, page, _), line_words in sorted(grouped.items(), key=lambda item: item[0]):
        if page is None:
            line_words.sort(key=lambda word: int(word.get("start", 0)))
            fragments = [line_words]
        else:
            line_words.sort(key=lambda word: float(word.get("x0", 0)))
            fragments = _split_row(line_words)
        for fragment in fragments:
            text = " ".join(str(word["text"]) for word in fragment).strip()
            if not text:
                continue
            if page is None:
                by_page[None].append(
                    _Line(
                        text=text,
                        page=None,
                        start=min(int(word["start"]) for word in fragment),
                        end=max(int(word["end"]) for word in fragment),
                    )
                )
            else:
                by_page[page].append(
                    _Line(
                        text=text,
                        page=int(page),
                        x0=min(float(word["x0"]) for word in fragment),
                        top=min(float(word["top"]) for word in fragment),
                        x1=max(float(word["x1"]) for word in fragment),
                        bottom=max(float(word["bottom"]) for word in fragment),
                        page_height=float(fragment[0].get("page_height", 0)) or None,
                    )
                )

    result: list[_Line] = []
    for page in sorted(by_page, key=lambda value: -1 if value is None else value):
        result.extend(_reading_order(by_page[page], words))
    return result


def _split_row(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Separate words sharing a baseline when a wide column gutter lies between them."""
    if not words:
        return []
    width = float(words[0].get("page_width", 0))
    fragments: list[list[dict[str, Any]]] = [[words[0]]]
    for word in words[1:]:
        previous = fragments[-1][-1]
        gap = float(word.get("x0", 0)) - float(previous.get("x1", 0))
        crosses_middle = (
            width
            and float(previous.get("x1", 0)) < width * 0.48
            and float(word.get("x0", 0)) > width * 0.52
        )
        if gap > max(18.0, width * 0.03) or crosses_middle:
            fragments.append([word])
        else:
            fragments[-1].append(word)
    return fragments


def _reading_order(lines: list[_Line], words: list[dict[str, Any]]) -> list[_Line]:
    if not lines or lines[0].page is None:
        return lines
    page = lines[0].page
    width = next(
        (
            float(word["page_width"])
            for word in words
            if word.get("page") == page and word.get("page_width")
        ),
        0.0,
    )
    if not width:
        return sorted(lines, key=lambda line: (line.top or 0, line.x0 or 0))

    left = [line for line in lines if (line.x1 or 0) < width * 0.6]
    right = [line for line in lines if (line.x0 or 0) > width * 0.4]
    if len(left) < 3 or len(right) < 3:
        return sorted(lines, key=lambda line: (line.top or 0, line.x0 or 0))

    spanning = [
        line
        for line in lines
        if width * 0.15 < (line.x0 or 0) < width * 0.4 and (line.x1 or 0) > width * 0.6
    ]
    ordered: list[_Line] = []
    lower = float("-inf")
    for anchor in sorted(spanning, key=lambda line: line.top or 0):
        upper = anchor.top or 0
        ordered.extend(_order_columns(lines, lower, upper, width, spanning))
        ordered.append(anchor)
        lower = anchor.bottom or upper
    ordered.extend(_order_columns(lines, lower, float("inf"), width, spanning))
    return ordered


def _order_columns(
    lines: list[_Line],
    lower: float,
    upper: float,
    width: float,
    spanning: list[_Line],
) -> list[_Line]:
    candidates = [
        line for line in lines if line not in spanning and lower <= (line.top or 0) < upper
    ]
    return sorted(
        candidates,
        key=lambda line: (0 if (line.x0 or 0) < width * 0.5 else 1, line.top or 0, line.x0 or 0),
    )


def _drop_running_matter(lines: list[_Line]) -> list[_Line]:
    page_count = len({line.page for line in lines if line.page is not None})
    edge_texts: Counter[str] = Counter()
    for line in lines:
        if _at_page_edge(line):
            edge_texts[_normal(line.text)] += 1
    repeated = {
        text for text, count in edge_texts.items() if page_count > 1 and count >= min(2, page_count)
    }
    return [
        line
        for line in lines
        if not _PAGE_NUMBER.fullmatch(line.text.strip())
        and not (_at_page_edge(line) and _normal(line.text) in repeated)
    ]


def _at_page_edge(line: _Line) -> bool:
    if line.page is None or line.top is None:
        return False
    if line.page_height:
        return (
            line.top <= line.page_height * 0.1
            or (line.bottom or line.top) >= line.page_height * 0.9
        )
    return line.top <= 72 or line.top >= 720


def _marker(text: str) -> tuple[str, str, bool] | None:
    stripped = text.strip()
    if match := _SECTION.match(stripped):
        return match.group(1), match.group(2).strip(), True
    if match := _PAREN.match(stripped):
        return match.group(1), match.group(2).strip(), False
    if match := _LETTER.match(stripped):
        return match.group(1), match.group(2).strip(), False
    if match := _NUMBER_PAREN.match(stripped):
        return match.group(1), match.group(2).strip(), False
    if match := _DECIMAL.match(stripped):
        return match.group(1).rstrip("."), match.group(2).strip(), False
    return None


def _heading_only(text: str) -> bool:
    if not text:
        return True
    if text.endswith("-") or len(text.split()) > 10 or _SENTENCE_END.search(text):
        return False
    ignored = {"und", "oder", "der", "die", "das", "des", "für", "von", "zu"}
    words = [word.strip("(),") for word in text.split()]
    return all(word.casefold() in ignored or word[:1].isupper() for word in words)


def _standalone_heading(text: str) -> bool:
    words = text.split()
    if not 1 <= len(words) <= 8 or _SENTENCE_END.search(text):
        return False
    letters = [word for word in words if any(character.isalpha() for character in word)]
    title_like = bool(letters) and all(word[0].isupper() for word in letters)
    heading_words = {
        "allgemeines",
        "anbieter",
        "eigentumsvorbehalt",
        "geltungsbereich",
        "geschäftsbedingungen",
        "gewährleistung",
        "haftung",
        "inhaltsverzeichnis",
        "lieferung",
        "mängelhaftung",
        "preise",
        "rückgabe",
        "schlussbestimmungen",
        "streitbeilegung",
        "versand",
        "vertragsschluss",
        "vertragspartner",
        "widerrufsbelehrung",
        "widerrufsrecht",
        "zahlung",
        "zahlungsbedingungen",
    }
    normalised = {word.casefold().strip("(),:;") for word in words}
    return title_like and any(
        heading_word in word for word in normalised for heading_word in heading_words
    )


def _join(lines: list[_Line]) -> str:
    result = ""
    for line in lines:
        current = line.text.strip()
        if not current:
            continue
        if result.endswith("-") and current[0].islower():
            result = result[:-1] + current
        else:
            result = f"{result} {current}".strip()
    return result


def _normal(text: str) -> str:
    return " ".join(text.casefold().split())

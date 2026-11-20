"""2.2 Normalize: the raw corpus CSV into one typed row per clause.

Row-level only. Anything that needs to look across a whole shop - the
foreign-law choice, for instance - belongs to the scope filter in 2.3.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from core.logging import get_logger

log = get_logger("p2.normalize")

# AGB-DE's topic vocabulary is wider than the agent's. Everything outside the
# prototype's remit maps to "other": carried and searchable, never privileged.
# See the Topic literal in core.schemas.
AGENT_TOPICS = {"liability", "warranty"}

# Shop sections are numbered either "10. Gewährleistung" or "§8 Haftung", and
# a few carry a number with no heading at all.
_TITLE = re.compile(r"^\s*(?:§\s*)?(\d+(?:\.\d+)*)\s*[.:)\-–—]*\s*(.*)$", re.DOTALL)

# A paragraph reference plus any Abs./Nr./Satz modifiers, and the law name when
# the clause bothers to give one.
_CITATION = re.compile(
    r"(§{1,2})\s*(\d+[a-z]?)"
    r"((?:\s*(?:Abs\.|Absatz|Nr\.|Satz|S\.|ff\.|f\.)\s*\d*)*)"
    r"\s*(BGB|HGB|UWG|ProdHaftG|TMG|UStG|EGBGB)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizeResult:
    path: Path
    rows: int
    fragments: int
    with_citations: int


def split_title(title: object) -> tuple[str | None, str | None]:
    """Section number and heading. Either side can be absent."""
    if not isinstance(title, str) or not title.strip():
        return None, None

    match = _TITLE.match(title.strip())
    if not match:
        return None, " ".join(title.split())

    number, heading = match.group(1), " ".join(match.group(2).split())
    return number, heading or None


def extract_citations(text: str) -> list[dict[str, str | None]]:
    """Statute references as the clause wrote them.

    Diagnostic only - never ground truth for retrieval. Two reasons: a bare
    low number is usually the shop citing its own section ("wie in § 10
    geregelt") rather than a statute, and the clauses that do cite a statute
    sometimes mean the HGB. The law is recorded only where it is stated, and
    left None rather than guessed.
    """
    citations = []
    for marker, paragraph, modifiers, law in _CITATION.findall(text):
        raw = f"{marker} {paragraph}{' ' + modifiers.strip() if modifiers.strip() else ''}"
        citations.append(
            {
                "raw": " ".join(raw.split()),
                "paragraph": paragraph,
                "law": law.upper() if law else None,
            }
        )
    return citations


def split_topics(value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return []
    return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]


def to_agent_topics(raw: list[str]) -> list[str]:
    """AGB-DE's vocabulary mapped onto core.schemas.Topic."""
    mapped = {topic if topic in AGENT_TOPICS else "other" for topic in raw}
    return sorted(mapped)


def normalize(
    corpus_path: Path,
    fragment_chars: int,
    out_dir: Path,
) -> NormalizeResult:
    corpus = pd.read_csv(corpus_path)

    rows = []
    for record in corpus.to_dict("records"):
        text = str(record["text"])
        topics_raw = split_topics(record.get("topics"))
        section_no, section_title = split_title(record.get("title"))

        rows.append(
            {
                "clause_id": f"agb-{int(record['id'])}",
                "source_id": int(record["id"]),
                "contract_id": int(record["contract"]),
                "lang": record.get("lang"),
                "section_no": section_no,
                "section_title": section_title,
                # Kept verbatim: the <<NAME>> placeholders are part of the
                # clause and the agent must cope with them.
                "text": text,
                "topics_raw": topics_raw,
                "topics": to_agent_topics(topics_raw),
                "subtopics": split_topics(record.get("subtopics")),
                "is_fragment": len(text.strip()) < fragment_chars,
                "cited_refs": extract_citations(text),
                # The label travels through interim and processed; only the
                # published clause table is forbidden to carry it.
                "label_void": bool(record["void"]),
            }
        )

    frame = pd.DataFrame(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "agb_clauses.parquet"
    frame.to_parquet(path, index=False)

    fragments = int(frame.is_fragment.sum())
    with_citations = int((frame.cited_refs.str.len() > 0).sum())
    log.info(
        "normalized %d clauses -> %s (%d fragments, %d citing a §)",
        len(frame),
        path.name,
        fragments,
        with_citations,
    )
    return NormalizeResult(
        path=path, rows=len(frame), fragments=fragments, with_citations=with_citations
    )

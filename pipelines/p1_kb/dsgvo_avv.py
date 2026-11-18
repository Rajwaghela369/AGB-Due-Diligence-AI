"""Build the AVV statute set without making a live index an accidental side effect.

EUR-Lex's public front end is protected by a browser challenge.  The Publications
Office content-negotiation endpoint exposes the same CELEX act as official German
XHTML and is therefore the reproducible acquisition boundary used here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from core import qdrant
from core.config import config_hash, load_yaml
from core.paths import CONFIGS, PROCESSED, RAW, STORES
from core.schemas import LawChunk
from lxml import etree

from pipelines.p1_kb.s4_build_index import IndexResult, build_index

CELEX = "32016R0679"
CANONICAL_URL = f"https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{CELEX}"
CONTENT_URL = f"https://publications.europa.eu/resource/celex/{CELEX}"
REUSE_URL = "https://commission.europa.eu/legal-notice_en#copyright-notice"
RAW_FILENAME = "dsgvo_32016R0679_de.xhtml"
SELECTED_DEFINITIONS = ("1", "2", "7", "8", "26")
ARTICLE_NUMBERS = (28, 32)
SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FetchResult:
    path: Path
    sha256: str
    retrieved_at: str


def _normalise(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def _element_text(element: etree._Element) -> str:
    return _normalise("".join(element.itertext()))


def _direct_children(element: etree._Element, name: str) -> list[etree._Element]:
    return element.xpath(f"./*[local-name()='{name}']")


def _article(root: etree._ElementTree, number: int) -> etree._Element:
    found = root.xpath(f"//*[local-name()='div' and @id='art_{number}']")
    if len(found) != 1:
        raise ValueError(f"official source contains {len(found)} copies of Article {number}")
    return found[0]


def fetch_official_dsgvo(out_dir: Path, *, session: Any = requests) -> FetchResult:
    """Snapshot the official German XHTML and its provenance manifest."""
    response = session.get(
        CONTENT_URL,
        headers={"Accept": "application/xhtml+xml", "Accept-Language": "de"},
        timeout=60,
    )
    response.raise_for_status()
    body = response.content
    root = etree.ElementTree(etree.fromstring(body))
    for number in (4, *ARTICLE_NUMBERS):
        _article(root, number)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / RAW_FILENAME
    path.write_bytes(body)
    retrieved_at = datetime.now(UTC).date().isoformat()
    digest = hashlib.sha256(body).hexdigest()
    manifest = {
        "celex": CELEX,
        "language": "de",
        "canonical_url": CANONICAL_URL,
        "content_url": CONTENT_URL,
        "retrieved_at": retrieved_at,
        "sha256": digest,
        "licence": "European Commission reuse policy (Decision 2011/833/EU)",
        "licence_url": REUSE_URL,
    }
    (out_dir / "dsgvo_source.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return FetchResult(path=path, sha256=digest, retrieved_at=retrieved_at)


def _title(article: etree._Element) -> str:
    titles = article.xpath("./*[local-name()='div' and contains(@class, 'eli-title')]//text()")
    return _normalise(" ".join(titles))


def _table_entry(table: etree._Element) -> tuple[str, str]:
    cells = table.xpath(".//*[local-name()='tr'][1]/*[local-name()='td']")
    if len(cells) < 2:
        raise ValueError("expected a label and body in an official-law list table")
    return _element_text(cells[0]).rstrip(".)"), _element_text(cells[1])


def _chunk(
    *,
    article: int,
    title: str,
    citation: str,
    chunk_id: str,
    text: str,
    item: str | None,
    sub_item: str | None,
    source_dump: str,
) -> LawChunk:
    return LawChunk(
        chunk_id=chunk_id,
        law="DSGVO",
        paragraph=f"Art. {article}",
        item=item,
        sub_item=sub_item,
        title=title,
        citation=citation,
        topics=["general"],
        embed_text=f"DSGVO {citation} {title}: {text}",
        text=text,
        source_dump=source_dump,
        source_url=CANONICAL_URL,
    )


def _definition_chunks(root: etree._ElementTree, source_dump: str) -> list[LawChunk]:
    article = _article(root, 4)
    title = _title(article)
    selected = set(SELECTED_DEFINITIONS)
    chunks: list[LawChunk] = []
    for table in _direct_children(article, "table"):
        number, text = _table_entry(table)
        if number not in selected:
            continue
        chunks.append(
            _chunk(
                article=4,
                title=title,
                citation=f"Art. 4 Nr. {number} DSGVO",
                chunk_id=f"dsgvo-4-nr{number}",
                text=text.rstrip(";."),
                item=number,
                sub_item=None,
                source_dump=source_dump,
            )
        )
    if {chunk.item for chunk in chunks} != selected:
        raise ValueError("one or more Art. 4 definitions required by Art. 28 are missing")
    return chunks


def _strip_absatz(text: str, absatz: str) -> str:
    return re.sub(rf"^\({re.escape(absatz)}\)\s*", "", text).strip()


def _numbered_article_chunks(
    root: etree._ElementTree, article_number: int, source_dump: str
) -> list[LawChunk]:
    article = _article(root, article_number)
    title = _title(article)
    chunks: list[LawChunk] = []
    paragraph_prefix = f"{article_number:03d}."

    for paragraph in _direct_children(article, "div"):
        paragraph_id = paragraph.get("id", "")
        if not paragraph_id.startswith(paragraph_prefix):
            continue
        absatz = paragraph_id.split(".", maxsplit=1)[1].lstrip("0") or "0"
        prose = [
            child
            for child in _direct_children(paragraph, "p")
            if "oj-normal" in child.get("class", "")
        ]
        prose_positions = {id(element): index for index, element in enumerate(prose, start=1)}

        for child in paragraph:
            local_name = etree.QName(child).localname
            if local_name == "p" and id(child) in prose_positions:
                prose_index = prose_positions[id(child)]
                text = _strip_absatz(_element_text(child), absatz)
                if not text:
                    continue
                underparagraph = f" UAbs. {prose_index}" if prose_index > 1 else ""
                suffix = f"-uabs{prose_index}" if prose_index > 1 else ""
                chunks.append(
                    _chunk(
                        article=article_number,
                        title=title,
                        citation=f"Art. {article_number} Abs. {absatz}{underparagraph} DSGVO",
                        chunk_id=f"dsgvo-{article_number}-abs{absatz}{suffix}",
                        text=text,
                        item=absatz,
                        sub_item=None,
                        source_dump=source_dump,
                    )
                )
            elif local_name == "table":
                letter, text = _table_entry(child)
                chunks.append(
                    _chunk(
                        article=article_number,
                        title=title,
                        citation=(f"Art. {article_number} Abs. {absatz} lit. {letter} DSGVO"),
                        chunk_id=f"dsgvo-{article_number}-abs{absatz}-lit-{letter}",
                        text=text.rstrip(";."),
                        item=absatz,
                        sub_item=letter,
                        source_dump=source_dump,
                    )
                )
    return chunks


def chunk_dsgvo(raw_path: Path, out_path: Path, *, retrieved_at: str) -> list[LawChunk]:
    """Create one chunk per paragraph or lettered item from the pinned XHTML."""
    root = etree.parse(raw_path)
    source_dump = f"CELEX:{CELEX}; retrieved={retrieved_at}"
    chunks = _definition_chunks(root, source_dump)
    for number in ARTICLE_NUMBERS:
        chunks.extend(_numbered_article_chunks(root, number, source_dump))

    ids = [chunk.chunk_id for chunk in chunks]
    if len(ids) != len(set(ids)):
        raise ValueError("DSGVO chunk ids are not unique")
    required = {f"Art. 28 Abs. 3 lit. {letter} DSGVO" for letter in "abcdefgh"}
    if not required.issubset({chunk.citation for chunk in chunks}):
        raise ValueError("Art. 28(3)(a)-(h) did not produce one chunk per letter")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(chunk.model_dump_json() + "\n")
    return chunks


def build_live_index(chunks_path: Path, kb: dict[str, Any], *, approved: bool) -> IndexResult:
    """Embed, upsert, and move the AVV alias only after explicit operator approval."""
    if not approved:
        raise PermissionError("live DSGVO indexing requires --approve-live-index")
    profile = kb["dsgvo_avv"]
    index_config = {
        "source": profile["source"],
        "embedding": kb["embedding"],
        "store": profile["store"],
        "chunking": profile["chunking"],
    }
    lineage = config_hash(index_config)
    result = build_index(
        chunks_path,
        kb=index_config,
        config_hash=lineage,
        manifest_dir=STORES / "dsgvo_avv",
    )
    qdrant.point_alias(
        qdrant.client(),
        alias=profile["store"]["alias"],
        collection=profile["store"]["collection"],
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true", help="refresh the official XHTML snapshot")
    parser.add_argument("--build-index", action="store_true", help="call Jina and Qdrant")
    parser.add_argument(
        "--approve-live-index",
        action="store_true",
        help="confirm that the orchestrator approved paid/live indexing",
    )
    args = parser.parse_args()

    raw_dir = RAW / "dsgvo_avv"
    manifest_path = raw_dir / "dsgvo_source.json"
    if args.fetch:
        fetched = fetch_official_dsgvo(raw_dir)
        retrieved_at = fetched.retrieved_at
    else:
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        retrieved_at = source["retrieved_at"]
    chunks_path = PROCESSED / "dsgvo_chunks.jsonl"
    chunk_dsgvo(raw_dir / RAW_FILENAME, chunks_path, retrieved_at=retrieved_at)

    if args.build_index:
        build_live_index(
            chunks_path,
            load_yaml(CONFIGS / "kb.yaml"),
            approved=args.approve_live_index,
        )


if __name__ == "__main__":
    main()

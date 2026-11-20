"""Small deterministic AVV source snapshots used by offline pipeline tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lxml import etree


def _table(parent: etree._Element, label: str, text: str) -> None:
    table = etree.SubElement(parent, "table")
    row = etree.SubElement(table, "tr")
    etree.SubElement(row, "td").text = label
    etree.SubElement(row, "td").text = text


def _statute_source() -> bytes:
    html = etree.Element("html")
    body = etree.SubElement(html, "body")

    article = etree.SubElement(body, "div", id="art_4")
    etree.SubElement(article, "div", **{"class": "eli-title"}).text = "Begriffsbestimmungen"
    definitions = {
        "1": "personenbezogene Daten",
        "2": "Verarbeitung",
        "7": "Verantwortlicher",
        "8": "Auftragsverarbeiter",
        "26": "Unternehmen",
    }
    for number, text in definitions.items():
        _table(article, number, text)

    article = etree.SubElement(body, "div", id="art_28")
    etree.SubElement(article, "div", **{"class": "eli-title"}).text = "Auftragsverarbeiter"
    paragraph = etree.SubElement(article, "div", id="028.003")
    for letter in "abcdefgh":
        _table(paragraph, letter, f"Pflicht nach Artikel 28 Absatz 3 Buchstabe {letter}")
    paragraph = etree.SubElement(article, "div", id="028.001")
    instruction = "Der Auftragsverarbeiter verarbeitet Daten nur nach Weisung."
    etree.SubElement(paragraph, "p", **{"class": "oj-normal"}).text = instruction

    article = etree.SubElement(body, "div", id="art_32")
    etree.SubElement(article, "div", **{"class": "eli-title"}).text = "Sicherheit der Verarbeitung"
    paragraph = etree.SubElement(article, "div", id="032.001")
    for letter in "abcd":
        _table(paragraph, letter, f"Geeignete Maßnahme nach Artikel 32 Buchstabe {letter}")
    for index in range(2, 16):
        requirement = f"Sicherheitsanforderung {index}."
        etree.SubElement(paragraph, "p", **{"class": "oj-normal"}).text = requirement

    return etree.tostring(html, encoding="utf-8", xml_declaration=True)


def _template_source() -> bytes:
    html = etree.Element("html")
    body = etree.SubElement(html, "body")
    annex = etree.SubElement(body, "div", id="anx_1")
    for ordinal in range(1, 56):
        etree.SubElement(annex, "p", **{"class": "oj-ti-grseq-1"}).text = f"Klausel {ordinal}"
        etree.SubElement(annex, "p", **{"class": "oj-ti-grseq-1"}).text = "Standardklausel"
        clause = f"Vertragliche Regelung Nummer {ordinal}."
        etree.SubElement(annex, "p", **{"class": "oj-normal"}).text = clause
    return etree.tostring(html, encoding="utf-8", xml_declaration=True)


def write_avv_sources(directory: Path) -> dict[str, Path]:
    """Write both pinned snapshots and manifests without network access."""
    directory.mkdir(parents=True, exist_ok=True)
    statute = directory / "dsgvo_32016R0679_de.xhtml"
    template = directory / "eu_scc_32021D0915_de.xhtml"
    statute_body = _statute_source()
    template_body = _template_source()
    statute.write_bytes(statute_body)
    template.write_bytes(template_body)
    common = {
        "language": "de",
        "retrieved_at": "2026-01-01",
        "licence": "European Commission reuse policy (Decision 2011/833/EU)",
        "licence_url": "https://commission.europa.eu/legal-notice_en#copyright-notice",
    }
    (directory / "dsgvo_source.json").write_text(
        json.dumps(
            {
                **common,
                "celex": "32016R0679",
                "canonical_url": "https://example.invalid/32016R0679",
                "content_url": "https://example.invalid/32016R0679",
                "sha256": hashlib.sha256(statute_body).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "eu_scc_source.json").write_text(
        json.dumps(
            {
                **common,
                "celex": "32021D0915",
                "canonical_url": "https://example.invalid/32021D0915",
                "content_url": "https://example.invalid/32021D0915",
                "sha256": hashlib.sha256(template_body).hexdigest(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {"statute": statute, "template": template}

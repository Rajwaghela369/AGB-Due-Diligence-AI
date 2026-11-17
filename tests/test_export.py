"""Golden exports pin the legal content and the machine-readable interchange format."""

import csv
import importlib
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from lxml import etree

from app.domain import Reviewer
from app.review import export as export_module
from app.review.interfaces import ExportBlocked
from tests.test_summary import NOW, seeded_service

GOLDEN = Path(__file__).parent / "golden" / "export"
EXPORT_DATE = datetime.fromisoformat("2026-11-17T18:30:00+01:00")


def _docx_text(content: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    lines = []
    for paragraph in root.xpath(".//w:p", namespaces=namespace):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=namespace)).strip()
        if text:
            lines.append(text)
    return "\n".join(lines) + "\n"


def _fixed_clock(monkeypatch) -> None:
    module = importlib.import_module("app.review.export")
    monkeypatch.setattr(module, "_now", lambda: EXPORT_DATE)
    monkeypatch.setattr("app.review.summary._now", lambda: NOW)


def test_markdown_csv_and_docx_match_goldens(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    _store, service = seeded_service(decide_uncertain=True)
    reviewer = Reviewer(name="Eva Prüferin")

    markdown = service.export("review-summary", "md", reviewer=reviewer)
    csv_file = service.export("review-summary", "csv", reviewer=reviewer)
    docx = service.export("review-summary", "docx", reviewer=reviewer)

    assert markdown.content.decode() == (GOLDEN / "memo.md").read_text()
    csv_text = csv_file.content.decode("utf-8-sig")
    assert csv_text.replace("\r\n", "\n") == (GOLDEN / "findings.csv").read_text()
    assert "\r\n" in csv_text and "\n" not in csv_text.replace("\r\n", "")
    assert _docx_text(docx.content) == (GOLDEN / "memo-docx.txt").read_text()
    assert markdown.filename == "AGB-Check_Muster-AGB_2026-11-17.md"
    assert csv_file.media_type == "text/csv; charset=utf-8"
    assert docx.content == service.export("review-summary", "docx", reviewer=reviewer).content
    DocxDocument(io.BytesIO(docx.content))  # python-docx can open the produced package


def test_final_export_is_blocked_in_document_order() -> None:
    _store, service = seeded_service()
    with pytest.raises(ExportBlocked) as caught:
        service.export("review-summary", "md", reviewer=Reviewer())
    assert caught.value.reason == "undecided"
    assert [blocker.position for blocker in caught.value.blocking] == [2]


def test_running_review_is_blocked_even_without_open_findings() -> None:
    store, service = seeded_service(decide_uncertain=True)
    store.reviews.set_status("review-summary", "running")
    with pytest.raises(ExportBlocked) as caught:
        service.export("review-summary", "csv", reviewer=Reviewer())
    assert caught.value.reason == "not_finished"
    assert caught.value.blocking == []


def test_draft_watermarks_all_formats(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    _store, service = seeded_service()
    reviewer = Reviewer()
    markdown = service.export("review-summary", "md", draft=True, reviewer=reviewer)
    csv_file = service.export("review-summary", "csv", draft=True, reviewer=reviewer)
    docx = service.export("review-summary", "docx", draft=True, reviewer=reviewer)

    assert markdown.content.startswith(b"> **ENTWURF**")
    assert ";Entwurf\r\n" in csv_file.content.decode("utf-8-sig")
    assert markdown.filename.endswith("_Entwurf.md")
    with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
        headers = b"".join(
            archive.read(name) for name in archive.namelist() if name.startswith("word/header")
        )
    assert b"rotation:315" in headers and b'string="Entwurf"' in headers
    assert b'<v:shapetype id="_x0000_t136"' in headers
    assert b'fitshape="t"' in headers and b"o:allowincell" in headers and b"<v:fill" in headers


def test_export_reuses_cached_summary_but_never_calls_model(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    calls = []

    def model(system, user):
        calls.append((system, user))
        raise AssertionError("an export must not call the summary model")

    _store, service = seeded_service(decide_uncertain=True, summary_model=model)
    exported = service.export("review-summary", "md", reviewer=Reviewer())
    assert calls == []
    assert b"KI-generierte Empfehlung" not in exported.content


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_neutralises_every_formula_prefix_after_leading_space(prefix) -> None:
    assert export_module._csv_safe(f"  {prefix}danger") == f"'{prefix}danger"


def test_csv_neutralises_untrusted_export_fields() -> None:
    _store, service = seeded_service(decide_uncertain=True)
    findings = service.findings("review-summary")
    first = findings[0]
    findings[0] = first.model_copy(
        update={
            "clause": first.clause.model_copy(
                update={"number": "=1+1", "heading": "+cmd", "text": " @formula"}
            ),
            "verdict": first.verdict.model_copy(update={"reason_de": "-reason"}),
            "decision": first.decision.model_copy(
                update={"note": "\tformula", "decided_by": "\rformula"}
            ),
        }
    )
    service.findings = lambda _review_id: findings

    exported = service.export("review-summary", "csv", reviewer=Reviewer())
    rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")), delimiter=";"))

    for field in ("nummer", "ueberschrift", "klausel", "begruendung", "notiz", "entschieden_von"):
        assert rows[0][field].startswith("'")


def test_docx_drops_xml_invalid_controls_from_every_source(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    store, service = seeded_service(decide_uncertain=True)
    document = store.documents.get("doc-summary")
    assert document is not None
    monkeypatch.setattr(
        store.documents,
        "get",
        lambda _document_id: document.model_copy(update={"filename": "A\x00GB.pdf"}),
    )
    findings = service.findings("review-summary")
    first = findings[0]
    findings[0] = first.model_copy(
        update={
            "clause": first.clause.model_copy(update={"text": "Text\x0cmit Steuerzeichen"}),
            "verdict": first.verdict.model_copy(update={"reason_de": "Grund\x07text"}),
            "decision": first.decision.model_copy(update={"note": "Notiz\x01text"}),
        }
    )
    service.findings = lambda _review_id: findings
    service.statutes["bgb-309-7"] = service.statutes["bgb-309-7"].model_copy(
        update={"citation": "§ 309\x02 BGB", "text": "Norm\x0btext"}
    )

    exported = service.export("review-summary", "docx", reviewer=Reviewer())
    text = _docx_text(exported.content)

    assert "AGB.pdf" in text and "Textmit Steuerzeichen" in text
    assert not any(char in text for char in ("\x00", "\x01", "\x02", "\x07", "\x0b", "\x0c"))


def test_final_export_lists_clauses_without_a_verdict() -> None:
    _store, service = seeded_service(decide_uncertain=True)

    markdown = service.export("review-summary", "md", reviewer=Reviewer()).content.decode()

    assert "5.1 – Diese Klausel konnte technisch nicht geprüft werden." in markdown
    assert "nicht geprüft" in markdown
    assert "6.1 – Allgemeine Angaben zum Vertragspartner." in markdown
    assert "nicht im Prüfumfang" in markdown


def test_override_and_dismiss_never_reuse_contradictory_model_reasoning() -> None:
    _store, service = seeded_service(decide_uncertain=True)

    markdown = service.export("review-summary", "md", reviewer=Reviewer()).content.decode()

    assert "Modell hielt die Klausel für wirksam." not in markdown
    assert "Die Klausel bestätigt das Gesetz." not in markdown
    assert markdown.count("Entscheidung der prüfenden Person, ohne Modellbegründung.") == 2


def test_markdown_normalises_newlines_and_escapes_filename_and_clause(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    store, service = seeded_service(decide_uncertain=True)
    document = store.documents.get("doc-summary")
    assert document is not None
    monkeypatch.setattr(
        store.documents,
        "get",
        lambda _document_id: document.model_copy(update={"filename": "A|<b>*x*.pdf"}),
    )
    findings = service.findings("review-summary")
    first = findings[0]
    findings[0] = first.model_copy(
        update={"clause": first.clause.model_copy(update={"text": "A\\|B\r\n`[*_]"})}
    )
    service.findings = lambda _review_id: findings

    markdown = service.export("review-summary", "md", reviewer=Reviewer()).content.decode()

    assert "\r" not in markdown
    assert "A\\|&lt;b&gt;\\*x\\*.pdf" in markdown
    assert r"A\\\|B<br>\`\[\*\_\]" in markdown


def test_cover_uses_berlin_review_day_and_separate_export_day(monkeypatch) -> None:
    _fixed_clock(monkeypatch)
    _store, service = seeded_service(decide_uncertain=True)
    review = service.get("review-summary")
    assert review is not None
    late_utc = datetime(2026, 11, 17, 23, 30, tzinfo=UTC)
    service.get = lambda _review_id: review.model_copy(update={"finished_at": late_utc})

    markdown = service.export("review-summary", "md", reviewer=Reviewer()).content.decode()

    assert "**Prüfdatum:** 18.11.2026" in markdown
    assert "**Exportiert am:** 17.11.2026" in markdown

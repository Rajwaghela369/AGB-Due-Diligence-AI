"""Golden exports pin the legal content and the machine-readable interchange format."""

import importlib
import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from lxml import etree

from app.domain import Reviewer
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
    assert ";ja\r\n" in csv_file.content.decode("utf-8-sig")
    assert markdown.filename.endswith("_Entwurf.md")
    with zipfile.ZipFile(io.BytesIO(docx.content)) as archive:
        headers = b"".join(
            archive.read(name) for name in archive.namelist() if name.startswith("word/header")
        )
    assert b"rotation:315" in headers and b'string="Entwurf"' in headers


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

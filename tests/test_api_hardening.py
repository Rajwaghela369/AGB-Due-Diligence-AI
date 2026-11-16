"""Every upload hazard the API must refuse, exercised through HTTP with a stable body.

Format is decided by the bytes, never the filename, so a mislabelled file is judged on its
content. Conversion through Gotenberg is replaced by a fake that returns a known PDF.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.rate_limit import IpRateLimiter
from app.ingest import validate
from app.review.feedback import FeedbackService
from app.store.memory import memory_store
from tests.test_api import FakeReviewService, _pdf


def _app(max_upload_mb: int = 20):
    store = memory_store()
    application = create_app(
        store=store,
        service=FakeReviewService(store),
        statutes={},
        feedback=FeedbackService(store, None),
        settings={"ingest": {"max_upload_mb": max_upload_mb}},
    )
    application.state.rate_limiter = IpRateLimiter(capacity=1000, per_minute=1000)
    return application


def _docx(parts: dict[str, bytes | str], *, compress: int = zipfile.ZIP_DEFLATED) -> bytes:
    members = {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": "<w:document/>",
        **parts,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compress) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


def _upload(client: TestClient, filename: str, data: bytes):
    return client.post("/api/v1/documents", files={"file": (filename, data)})


def test_malformed_pdf_is_refused_with_a_stable_body():
    application = _app()
    broken = b"%PDF-1.4\nthis is not a pdf at all\n%%EOF\n"
    with TestClient(application) as client:
        response = _upload(client, "terms.pdf", broken)

    assert response.status_code == 422
    assert response.json()["code"] == "parse_failed"
    assert response.json()["message"]  # German, from the ingest layer


def test_oversized_multipart_upload_is_413_before_parsing(monkeypatch):
    application = _app(max_upload_mb=1)
    monkeypatch.setattr(
        "app.api.routes.documents.ingest_file",
        lambda *a, **k: pytest.fail("an oversized upload must not reach ingest"),
    )
    with TestClient(application) as client:
        response = _upload(client, "terms.pdf", b"%PDF-" + b"x" * (1024 * 1024))

    assert response.status_code == 413
    assert response.json()["code"] == "too_large"
    assert "Upload-Größe" in response.json()["message"]


def test_docx_with_macros_is_415_with_a_clear_message():
    application = _app()
    macro_docx = _docx({"word/vbaProject.bin": b"\x00macro"})
    with TestClient(application) as client:
        response = _upload(client, "vertrag.docx", macro_docx)

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_type"
    assert "Makros" in response.json()["message"]


def test_docx_named_pdf_is_judged_by_its_bytes(monkeypatch):
    application = _app()
    converted: list[bytes] = []
    monkeypatch.setattr(
        "app.ingest.convert.convert_docx",
        lambda data, filename, **kwargs: converted.append(data) or _pdf(),
    )
    with TestClient(application) as client:
        response = _upload(client, "vertrag.docx", _docx({}))
        mislabelled = _upload(client, "vertrag.docx", _pdf())

    assert response.status_code == 200
    assert response.json()["document"]["source_type"] == "docx"
    assert converted, "a DOCX must go through the converter"
    assert mislabelled.status_code == 200
    assert mislabelled.json()["document"]["source_type"] == "pdf"


def test_pdf_named_docx_is_judged_by_its_bytes(monkeypatch):
    application = _app()
    converted: list[bytes] = []
    monkeypatch.setattr(
        "app.ingest.convert.convert_docx",
        lambda data, filename, **kwargs: converted.append(data) or _pdf(),
    )
    with TestClient(application) as client:
        response = _upload(client, "terms.docx", _pdf())

    assert response.status_code == 200
    assert response.json()["document"]["source_type"] == "pdf"
    assert not converted, "a PDF must never be sent to the DOCX converter"


def test_docx_named_pdf_on_non_zip_bytes_is_415():
    application = _app()
    with TestClient(application) as client:
        response = _upload(client, "terms.pdf", b"PK\x03\x04 but not a zip archive")

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_type"


def test_zip_bomb_disguised_as_docx_is_refused_by_its_declared_expansion(monkeypatch):
    application = _app()
    monkeypatch.setattr(
        "app.ingest.convert.convert_docx",
        lambda *a, **k: pytest.fail("a zip bomb must never reach the converter"),
    )
    # 4 MB of zeros deflates to about 4 KB: a ratio near 1000:1, far above the 100:1 limit.
    bomb = _docx({"word/document.xml": b"\x00" * (4 * 1024 * 1024)})
    assert len(bomb) < 64 * 1024
    with TestClient(application) as client:
        response = _upload(client, "vertrag.docx", bomb)

    assert response.status_code == 413
    assert response.json()["code"] == "archive_too_large"
    assert "Entpacken" in response.json()["message"]


def test_archive_over_the_size_cap_is_refused_before_any_member_is_read(monkeypatch):
    application = _app()
    archive = _docx({"word/styles.xml": b"x" * 4096})
    monkeypatch.setattr(validate, "MAX_ARCHIVE_UNCOMPRESSED_BYTES", 1024)
    monkeypatch.setattr(
        zipfile.ZipFile,
        "open",
        lambda *a, **k: pytest.fail("no member may be opened before the budget check"),
    )
    with TestClient(application) as client:
        response = _upload(client, "vertrag.docx", archive)

    assert response.status_code == 413
    assert response.json()["code"] == "archive_too_large"


def test_archive_with_too_many_entries_is_refused(monkeypatch):
    application = _app()
    monkeypatch.setattr(validate, "MAX_ARCHIVE_ENTRIES", 3)
    many = {f"word/part{index}.xml": "<x/>" for index in range(5)}
    with TestClient(application) as client:
        response = _upload(client, "vertrag.docx", _docx(many))

    assert (response.status_code, response.json()["code"]) == (413, "archive_too_large")


def test_every_refusal_has_the_error_body_shape():
    application = _app()
    cases = [
        ("terms.pdf", b""),
        ("terms.pdf", b"plain text"),
        ("vertrag.docx", _docx({"word/vbaProject.bin": b"x"})),
    ]
    with TestClient(application) as client:
        responses = [_upload(client, name, data) for name, data in cases]

    for response in responses:
        assert response.status_code >= 400
        assert set(response.json()) == {"code", "message"}
        assert response.json()["code"]

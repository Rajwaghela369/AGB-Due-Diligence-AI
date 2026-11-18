"""Ingest failures and cache behavior stay deterministic without external services."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest

from app.ingest import IngestError, ingest_file, ingest_text
from app.ingest.convert import convert_docx
from app.ingest.parse import parse_pdf
from app.ingest.validate import validate_upload


def _pdf(text: str | None = "§ 1 Test") -> bytes:
    stream = b"" if text is None else f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(payload)


def _docx(*, macro: bool = False) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<w:document/>")
        if macro:
            archive.writestr("word/vbaProject.bin", b"macro")
    return output.getvalue()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "empty"),
        (b"not a document", "unsupported_type"),
        (_docx(macro=True), "unsupported_type"),
        (_pdf(None), "no_text_layer"),
    ],
)
def test_validation_error_codes(payload, code):
    with pytest.raises(IngestError) as raised:
        validate_upload(payload)
    assert raised.value.code == code


def test_upload_size_limit_has_stable_code():
    with pytest.raises(IngestError) as raised:
        validate_upload(_pdf(), limit_bytes=4)
    assert raised.value.code == "too_large"


def test_encrypted_pdf_has_stable_code(monkeypatch):
    def reject(data):
        raise IngestError("encrypted", "encrypted")

    monkeypatch.setattr("app.ingest.validate.validate_pdf", reject)
    with pytest.raises(IngestError) as raised:
        validate_upload(_pdf())
    assert raised.value.code == "encrypted"


def test_malformed_pdf_maps_to_parse_failed():
    with pytest.raises(IngestError) as raised:
        validate_upload(b"%PDF-broken")
    assert raised.value.code == "parse_failed"


def test_content_detection_ignores_the_filename():
    assert validate_upload(_pdf()) == "pdf"
    assert validate_upload(_docx()) == "docx"


def test_convert_posts_to_gotenberg_with_timeout():
    seen = {}

    class Response:
        content = _pdf()

        def raise_for_status(self):
            return None

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    result = convert_docx(
        _docx(), "terms.docx", http_post=post, base_url="http://converter", timeout=7
    )

    assert result.startswith(b"%PDF-")
    assert seen["url"] == "http://converter/forms/libreoffice/convert"
    assert seen["timeout"] == 7
    assert seen["files"]["files"][0] == "terms.docx"


def test_conversion_failure_has_stable_code():
    def post(*args, **kwargs):
        raise TimeoutError

    with pytest.raises(IngestError) as raised:
        convert_docx(_docx(), "terms.docx", http_post=post)
    assert raised.value.code == "conversion_failed"


def test_generated_pdf_is_parsed_in_a_subprocess():
    words, pages = parse_pdf(_pdf("6. Haftung gilt."), timeout=10)

    assert pages == 1
    assert " ".join(word["text"] for word in words) == "6. Haftung gilt."
    assert all({"page", "x0", "top", "x1", "bottom", "line"} <= word.keys() for word in words)


def test_parse_timeout_has_stable_code(monkeypatch):
    class Endpoint:
        def poll(self, timeout):
            return False

        def close(self):
            return None

    class Process:
        def start(self):
            return None

        def kill(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    context = SimpleNamespace(
        Pipe=lambda duplex: (Endpoint(), Endpoint()), Process=lambda **kwargs: Process()
    )
    monkeypatch.setattr("app.ingest.parse.multiprocessing.get_context", lambda method: context)

    with pytest.raises(IngestError) as raised:
        parse_pdf(_pdf(), timeout=0)
    assert raised.value.code == "parse_timeout"


class _Files:
    def __init__(self):
        self.data = {}

    def put(self, key, data):
        self.data.setdefault(key, data)
        return key

    def exists(self, key):
        return key in self.data


class _Documents:
    def __init__(self):
        self.parsed = {}
        self.saved_clauses = {}
        self.uploads = []

    def find_parsed(self, document_id, parser_version, splitter_version):
        return self.parsed.get((document_id, parser_version, splitter_version))

    def save(self, document, clauses, texts):
        key = (document.document_id, document.parser_version, document.splitter_version)
        self.parsed[key] = document
        self.saved_clauses[document.document_id] = clauses

    def clauses(self, document_id):
        return self.saved_clauses[document_id]

    def record_upload(
        self, document_id, filename, source_type, size, document_type="agb", *, owner_id="local"
    ):
        self.uploads.append((document_id, filename, source_type, size, document_type))
        return str(len(self.uploads))


def test_second_ingest_uses_parse_cache(monkeypatch):
    store = SimpleNamespace(files=_Files(), documents=_Documents())
    calls = []

    def parser(data):
        calls.append(data)
        return (
            [
                {
                    "text": "1.",
                    "page": 1,
                    "line": 0,
                    "x0": 10,
                    "top": 100,
                    "x1": 20,
                    "bottom": 110,
                },
                {
                    "text": "Klausel.",
                    "page": 1,
                    "line": 0,
                    "x0": 22,
                    "top": 100,
                    "x1": 60,
                    "bottom": 110,
                },
            ],
            1,
        )

    monkeypatch.setattr("app.ingest.parse.parse_pdf", parser)
    first = ingest_file(_pdf(), "first.pdf", store=store)
    second = ingest_file(_pdf(), "again.docx", store=store)

    assert not first.reused
    assert second.reused
    assert second.document == first.document
    assert len(calls) == 1
    assert len(store.documents.uploads) == 2


def test_empty_pasted_text_has_stable_code():
    store = SimpleNamespace(files=_Files(), documents=_Documents())
    with pytest.raises(IngestError) as raised:
        ingest_text(" \n ", "paste", store=store)
    assert raised.value.code == "empty"


def test_a_reuploaded_docx_hits_the_cache_before_conversion(monkeypatch):
    """Gotenberg's output is not byte-stable, so the key must be the DOCX itself."""
    import itertools

    import app.ingest.convert as convert
    from app.ingest import ingest_file
    from app.store.memory import memory_store

    stamp = itertools.count()
    calls = []

    def fake_convert(data, filename, **_):
        calls.append(filename)
        return _pdf(f"§ 1 Haftung. Version {next(stamp)} der Umwandlung.")

    monkeypatch.setattr(convert, "convert_docx", fake_convert)
    store = memory_store()
    docx = _docx()
    first = ingest_file(docx, "agb.docx", store=store)
    second = ingest_file(docx, "agb.docx", store=store)
    assert second.reused and second.document.document_id == first.document.document_id
    assert calls == ["agb.docx"]

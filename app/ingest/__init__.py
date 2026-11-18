"""File to clauses with positions. Implemented in validate, convert, parse and split.

Public surface (the Phase 5 contract):

    ingest_file(data, filename, *, store, document_type="agb") -> IngestResult
    ingest_text(text, filename, *, store, document_type="agb") -> IngestResult
    split(words) -> list[ClauseSpan]           # pure, unit-testable
    IngestError(code, message)                 # code is shown by the UI

`IngestError.code` is one of: "too_large", "unsupported_type",
"archive_too_large", "encrypted", "no_text_layer", "conversion_failed", "parse_failed",
"parse_timeout", "empty".
"""

from __future__ import annotations

import hashlib

from core.hashing import content_hash

from app.domain import Document, DocumentType, IngestResult, blob_key, document_id_for

PARSER_VERSION = "pdfplumber-1"
SPLITTER_VERSION = "2"  # bump on any change to split.py


class IngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def ingest_file(
    data: bytes, filename: str, *, store, document_type: DocumentType = "agb"
) -> IngestResult:
    """Validate and content-address one uploaded PDF or DOCX."""
    from app.ingest.convert import convert_docx
    from app.ingest.split import split
    from app.ingest.validate import detect_upload, validate_pdf

    source_type = detect_upload(data)
    # Keyed on the uploaded bytes, and checked before conversion: Gotenberg stamps
    # each PDF it renders, so the converted bytes differ on every upload of a DOCX.
    source_hash = hashlib.sha256(data).hexdigest()
    document_id = document_id_for(source_hash, document_type=document_type)

    cached = store.documents.find_parsed(document_id, PARSER_VERSION, SPLITTER_VERSION)
    if cached is not None:
        store.documents.record_upload(document_id, filename, source_type, len(data), document_type)
        return IngestResult(
            document=cached,
            clauses=store.documents.clauses(document_id),
            reused=True,
        )

    pdf_data = convert_docx(data, filename) if source_type == "docx" else data
    words, page_count = validate_pdf(pdf_data)
    clauses = split(words)
    if not clauses:
        raise IngestError("empty", "Das Dokument enthält keine prüfbaren Klauseln.")

    document = Document(
        document_id=document_id,
        filename=filename,
        source_type=source_type,
        page_count=page_count,
        parser_version=PARSER_VERSION,
        splitter_version=SPLITTER_VERSION,
        text_hash=content_hash("\n".join(clause.text for clause in clauses)),
        document_type=document_type,
        source_hash=source_hash,
    )
    _save_with_blob(store, document, clauses, blob_key(document), pdf_data)
    store.documents.record_upload(document_id, filename, source_type, len(data), document_type)
    return IngestResult(document=document, clauses=clauses)


def ingest_text(
    text: str, filename: str, *, store, document_type: DocumentType = "agb"
) -> IngestResult:
    """Content-address pasted text and split it with character offsets."""
    from app.ingest.split import split, words_from_text

    if not text.strip():
        raise IngestError("empty", "Der eingefügte Text ist leer.")

    data = text.encode("utf-8")
    source_hash = hashlib.sha256(data).hexdigest()
    document_id = document_id_for(source_hash, document_type=document_type)
    cached = store.documents.find_parsed(document_id, PARSER_VERSION, SPLITTER_VERSION)
    if cached is not None:
        store.documents.record_upload(document_id, filename, "text", len(data), document_type)
        return IngestResult(
            document=cached,
            clauses=store.documents.clauses(document_id),
            reused=True,
        )

    clauses = split(words_from_text(text))
    if not clauses:
        raise IngestError("empty", "Der eingefügte Text enthält keine prüfbaren Klauseln.")
    document = Document(
        document_id=document_id,
        filename=filename,
        source_type="text",
        parser_version=PARSER_VERSION,
        splitter_version=SPLITTER_VERSION,
        text_hash=content_hash(text),
        document_type=document_type,
        source_hash=source_hash,
    )
    _save_with_blob(store, document, clauses, blob_key(document), data)
    store.documents.record_upload(document_id, filename, "text", len(data), document_type)
    return IngestResult(document=document, clauses=clauses)


def _save_with_blob(store, document: Document, clauses: list, key: str, data: bytes) -> None:
    """Blob first, then the document; then make sure the blob is still there.

    A blob erasure queued by an earlier deletion or the orphan sweep may run between the
    two writes: `put` was a no-op because the key existed, and the erasure (which waits for
    this save under the deletion lock, `DeletionRepo.erase_blob`) removed it just before
    the document was committed. Once the document is saved, no erasure touches the key
    again, so one check afterwards suffices.
    """
    store.files.put(key, data)
    texts = {clause.content_hash: clause.text for clause in clauses}
    store.documents.save(document, clauses, texts)
    if not store.files.exists(key):
        store.files.put(key, data)


from app.ingest.split import split  # noqa: E402  (public contract re-export)

__all__ = [
    "IngestError",
    "PARSER_VERSION",
    "SPLITTER_VERSION",
    "ingest_file",
    "ingest_text",
    "split",
]

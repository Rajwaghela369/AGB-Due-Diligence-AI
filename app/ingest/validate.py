"""Reject dangerous or unusable uploads before conversion and parsing.

Extensions are attacker-controlled, so the accepted type is derived solely from the
payload. DOCX macros are rejected before LibreOffice ever sees the archive.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Literal

from core.config import load_yaml
from core.paths import CONFIGS

from app.ingest import IngestError

DEFAULT_MAX_UPLOAD_MB = 20


def max_upload_bytes() -> int:
    """Read the optional app limit, retaining the documented 20 MB default."""
    path = CONFIGS / "app.yaml"
    config = load_yaml(path) if path.exists() else {}
    value = config.get("ingest", {}).get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB)
    return int(value) * 1024 * 1024


def validate_upload(data: bytes, *, limit_bytes: int | None = None) -> Literal["pdf", "docx"]:
    """Return the content-derived source type or raise a stable ``IngestError``."""
    source_type = detect_upload(data, limit_bytes=limit_bytes)
    if source_type == "pdf":
        validate_pdf(data)
    return source_type


def detect_upload(data: bytes, *, limit_bytes: int | None = None) -> Literal["pdf", "docx"]:
    """Perform cheap checks that are safe to repeat before a parse-cache lookup."""
    if not data:
        raise IngestError("empty", "Die hochgeladene Datei ist leer.")
    if len(data) > (limit_bytes if limit_bytes is not None else max_upload_bytes()):
        raise IngestError("too_large", "Die Datei überschreitet die zulässige Upload-Größe.")

    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK"):
        _validate_docx(data)
        return "docx"
    raise IngestError("unsupported_type", "Unterstützt werden PDF- und DOCX-Dateien.")


def validate_pdf(data: bytes):
    """Inspect encryption and text in the resource-limited parser worker."""
    from app.ingest.parse import parse_pdf

    return parse_pdf(data)


def _validate_docx(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = {Path(name).as_posix().lower() for name in archive.namelist()}
            content_types = archive.read("[Content_Types].xml").lower()
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise IngestError("unsupported_type", "Die Datei ist kein gültiges DOCX.") from exc

    if "word/document.xml" not in names:
        raise IngestError("unsupported_type", "Die Datei ist kein gültiges DOCX.")
    if b"macroenabled" in content_types or any(name.endswith("vbaproject.bin") for name in names):
        raise IngestError("unsupported_type", "DOCX-Dateien mit Makros werden nicht unterstützt.")

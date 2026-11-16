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

# DOCX is a ZIP, so its size on the wire says little about what LibreOffice will expand.
# These limits are checked from the central directory, before any member is decompressed.
MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
RATIO_CHECK_FLOOR_BYTES = 1024 * 1024  # tiny archives compress well for innocent reasons
MAX_PART_BYTES = 1024 * 1024  # the content-types part is small in any real document


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
            _check_archive_budget(archive.infolist(), len(data))
            names = {Path(name).as_posix().lower() for name in archive.namelist()}
            if "word/document.xml" not in names:
                raise IngestError("unsupported_type", "Die Datei ist kein gültiges DOCX.")
            if any(name.endswith("vbaproject.bin") for name in names):
                raise IngestError(
                    "unsupported_type", "DOCX-Dateien mit Makros werden nicht unterstützt."
                )
            content_types = _read_part(archive, "[Content_Types].xml").lower()
    except IngestError:
        raise
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise IngestError("unsupported_type", "Die Datei ist kein gültiges DOCX.") from exc

    if b"macroenabled" in content_types:
        raise IngestError("unsupported_type", "DOCX-Dateien mit Makros werden nicht unterstützt.")


def _check_archive_budget(infos: list[zipfile.ZipInfo], compressed_size: int) -> None:
    """Refuse archives whose declared expansion is out of proportion, before reading them."""
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise _too_expanded()
    uncompressed = sum(info.file_size for info in infos)
    if uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise _too_expanded()
    if (
        uncompressed > RATIO_CHECK_FLOOR_BYTES
        and uncompressed > compressed_size * MAX_COMPRESSION_RATIO
    ):
        raise _too_expanded()


def _read_part(archive: zipfile.ZipFile, name: str) -> bytes:
    """Read one member, but never more than the part limit, whatever its header claims."""
    with archive.open(name) as part:
        content = part.read(MAX_PART_BYTES + 1)
    if len(content) > MAX_PART_BYTES:
        raise _too_expanded()
    return content


def _too_expanded() -> IngestError:
    return IngestError(
        "archive_too_large",
        "Die DOCX-Datei ist nach dem Entpacken zu groß.",
    )

"""Convert DOCX bytes through Gotenberg without coupling ingest to HTTP in tests."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import requests

from app.ingest import IngestError

DEFAULT_GOTENBERG_URL = "http://localhost:3000"
DEFAULT_TIMEOUT_SECONDS = 30.0


def convert_docx(
    data: bytes,
    filename: str,
    *,
    http_post: Callable[..., Any] = requests.post,
    base_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Return Gotenberg's PDF response, mapping every transport failure to the contract."""
    url = f"{(base_url or os.getenv('GOTENBERG_URL', DEFAULT_GOTENBERG_URL)).rstrip('/')}"
    url += "/forms/libreoffice/convert"
    try:
        response = http_post(
            url,
            files={
                "files": (
                    filename,
                    data,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            timeout=timeout,
        )
        response.raise_for_status()
    except (requests.RequestException, TimeoutError, OSError) as exc:
        raise IngestError(
            "conversion_failed", "Die DOCX-Datei konnte nicht konvertiert werden."
        ) from exc

    pdf = bytes(response.content)
    if not pdf.startswith(b"%PDF-"):
        raise IngestError("conversion_failed", "Der Konvertierungsdienst lieferte kein PDF.")
    return pdf

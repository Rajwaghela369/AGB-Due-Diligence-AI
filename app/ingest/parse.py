"""Extract positioned PDF words outside the API process.

The parent enforces a wall-clock deadline on every platform. Linux workers additionally
set ``RLIMIT_AS`` before opening the PDF; macOS does not reliably enforce that limit, so
the process boundary and deadline are the effective protections there.
"""

from __future__ import annotations

import io
import multiprocessing
import sys
from multiprocessing.connection import Connection
from typing import Any

from app.ingest import IngestError

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MEMORY_MB = 512


def parse_pdf(
    data: bytes,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    memory_mb: int = DEFAULT_MEMORY_MB,
) -> tuple[list[dict[str, Any]], int]:
    """Return positioned words and page count, killing a worker that misses its deadline."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(data, child, memory_mb))
    process.start()
    child.close()
    try:
        if not parent.poll(timeout):
            process.kill()
            process.join()
            raise IngestError(
                "parse_timeout", "Das PDF konnte nicht rechtzeitig verarbeitet werden."
            )
        kind, payload = parent.recv()
    except EOFError as exc:
        raise IngestError("parse_failed", "Das PDF konnte nicht verarbeitet werden.") from exc
    finally:
        parent.close()
        if process.is_alive():
            process.join(1)
        if process.is_alive():
            process.kill()
        process.join()

    if kind == "ingest_error":
        code, message = payload
        raise IngestError(code, message)
    if kind == "error":
        raise IngestError("parse_failed", "Das PDF konnte nicht verarbeitet werden.")
    words, page_count = payload
    if not words:
        raise IngestError(
            "no_text_layer", "Das PDF enthält keine Textebene. Bitte führen Sie zuerst OCR aus."
        )
    return words, page_count


def _worker(data: bytes, connection: Connection, memory_mb: int) -> None:
    try:
        _set_memory_limit(memory_mb)
        connection.send(("ok", _extract(data)))
    except IngestError as exc:
        connection.send(("ingest_error", (exc.code, exc.message)))
    except BaseException as exc:
        if type(exc).__name__ == "PDFPasswordIncorrect":
            connection.send(
                (
                    "ingest_error",
                    ("encrypted", "Passwortgeschützte PDFs werden nicht unterstützt."),
                )
            )
        else:
            connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def _set_memory_limit(memory_mb: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    limit = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _extract(data: bytes) -> tuple[list[dict[str, Any]], int]:
    import pdfplumber

    result: list[dict[str, Any]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if getattr(pdf.doc, "encryption", None):
            raise IngestError("encrypted", "Passwortgeschützte PDFs werden nicht unterstützt.")
        for page_number, page in enumerate(pdf.pages, start=1):
            page_words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            line_tops: list[float] = []
            for word in page_words:
                top = float(word["top"])
                line = next(
                    (index for index, seen_top in enumerate(line_tops) if abs(seen_top - top) <= 3),
                    None,
                )
                if line is None:
                    line = len(line_tops)
                    line_tops.append(top)
                result.append(
                    {
                        "text": word["text"],
                        "page": page_number,
                        "x0": float(word["x0"]),
                        "top": top,
                        "x1": float(word["x1"]),
                        "bottom": float(word["bottom"]),
                        "line": line,
                        "page_width": float(page.width),
                        "page_height": float(page.height),
                    }
                )
        return result, len(pdf.pages)

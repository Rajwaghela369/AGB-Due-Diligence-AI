"""File to clauses with positions. Implemented in validate, convert, parse and split.

Public surface (the Phase 5 contract):

    ingest_file(data, filename, *, store) -> IngestResult
    ingest_text(text, filename, *, store) -> IngestResult
    split(words) -> list[ClauseSpan]           # pure, unit-testable
    IngestError(code, message)                 # code is shown by the UI

`IngestError.code` is one of: "too_large", "unsupported_type", "encrypted",
"no_text_layer", "conversion_failed", "parse_failed", "parse_timeout", "empty".
"""

PARSER_VERSION = "pdfplumber-1"
SPLITTER_VERSION = "1"  # bump on any change to split.py


class IngestError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

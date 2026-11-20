"""Content addressing for clauses.

A clause is identified by the hash of its text, so re-uploading the same
contract stores nothing new and a verdict already reached can be served from
the table instead of the model. Phase 2 found 11 exact-duplicate clauses across
the split - `agb-2808` and `agb-3123` are word-for-word identical in different
shops - and those collapse to one row.

Normalisation stops at Unicode NFC and whitespace collapse, deliberately.
Anything more aggressive merges clauses that differ in a word that decides the
verdict: "nur bei Vorsatz" and "nur bei Vorsatz und grober Fahrlässigkeit" are
different clauses with different answers. Anything less lets a stray double
space defeat the dedup.
"""

import hashlib
import unicodedata


def normalise(text: str) -> str:
    """NFC, then collapse every run of whitespace to a single space."""
    return " ".join(unicodedata.normalize("NFC", text).split())


def content_hash(text: str) -> str:
    """Stable sha256 of the normalised clause text."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()

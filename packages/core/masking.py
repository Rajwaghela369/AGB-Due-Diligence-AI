"""The masking boundary: personal data never leaves the server unmasked (Phase 9, 9.A).

Every byte of client text that goes to a model provider (Gemini through `core.llm`, Jina
through `core.embeddings` and `core.routing`) or to Langfuse passes through the one
`MaskingBoundary` here, so there is a single place to get right and a single place to test.
docs/Phase 9 Design.md explains the choices; in short:

- **Where.** Inside the outbound functions themselves, not at their call sites. A call
  site that forgets to mask cannot exist, because no call site masks: `core.llm.decide`
  masks `text`, `core.llm.complete` masks `user`, the Jina functions mask their inputs
  after computing cache keys, and Langfuse gets `langfuse_mask` as its `mask=` hook.
  System prompts are not masked: they are our own versioned templates and never carry
  client text (a contract every prompt builder keeps).
- **Proof.** A masked string is a `MaskedText`, a `str` subclass. Concatenating or slicing
  it gives a plain `str` again, so anything built *after* masking is unmasked by type. A
  strict client (the test fakes) calls `require_masked` on what it receives and refuses
  plain strings while masking is enabled.
- **Reversible, for display only.** `MaskedText.placeholders` maps each placeholder
  (`<PERSON_1>`) back to the original. The map lives only in memory for the duration of
  one call; it is never stored or traced. `unmask` restores a model's answer that quotes
  placeholders before the lawyer sees it.
- **Cache identity stays on the unmasked text.** `core.hashing.content_hash` runs on the
  original clause, so a verdict cached before masking is still found after it.

One module-level boundary, configured once at start-up (`configure`), rather than a masker
threaded through every call: `core.llm` is called from the agent graph, the eval harness and
the review service, and a global boundary is the only way none of them can skip it. Tests
reset it with `configure(None)`.

Wave 0 ships the contract: the types, the boundary and its checks. The Presidio masker is a
stub until Wave 1 (masking slice).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# What 9.A masks. Placeholders are `<KIND_n>`, numbered per masked text in order of first
# appearance, so the same name twice in one clause is one placeholder.
EntityKind = Literal["PERSON", "ADDRESS", "EMAIL", "PHONE", "IBAN", "REGISTER_NUMBER"]
ENTITY_KINDS: tuple[EntityKind, ...] = (
    "PERSON",
    "ADDRESS",
    "EMAIL",
    "PHONE",
    "IBAN",
    "REGISTER_NUMBER",
)
PLACEHOLDER = re.compile(r"<(PERSON|ADDRESS|EMAIL|PHONE|IBAN|REGISTER_NUMBER)_(\d+)>")


def placeholder(kind: EntityKind, number: int) -> str:
    return f"<{kind}_{number}>"


class MaskedText(str):
    """A string that went through the masker, with the way back.

    Being a `str`, it goes wherever the text went before (a request body, a prompt), so no
    provider call changes shape. Any operation that builds a new string (`+`, `format`,
    slicing) returns a plain `str`: the marker survives only on exactly what was masked.
    """

    placeholders: Mapping[str, str]

    def __new__(cls, text: str, placeholders: Mapping[str, str] | None = None) -> MaskedText:
        masked = super().__new__(cls, text)
        masked.placeholders = dict(placeholders or {})
        return masked

    def unmask(self, text: str | None = None) -> str:
        """`text` (a model's answer quoting placeholders; this text when None) with every
        known placeholder replaced by its original. Unknown placeholders stay as they are."""
        source = str(self) if text is None else text
        return PLACEHOLDER.sub(lambda m: self.placeholders.get(m.group(0), m.group(0)), source)


class Masker(Protocol):
    """Finds personal data in a text and replaces it with placeholders.

    `mask` must be deterministic for a given text and configuration (golden tests, and
    the same prompt for the same clause), must never return the original text for a span
    it detected, and must raise rather than return a half-masked text when it fails.
    """

    def mask(self, text: str) -> MaskedText: ...


class MaskingError(RuntimeError):
    """The masker failed. The text is not sent: the provider call fails like any other
    provider error (a failed result, not a crash)."""


class UnmaskedPayload(RuntimeError):
    """A strict client received text that did not pass the boundary while masking is on."""


class IdentityMasker:
    """Masks nothing but marks the text as having passed the boundary. For tests of the
    plumbing, and for a deployment that has decided (documented) to send text as is."""

    def mask(self, text: str) -> MaskedText:
        return MaskedText(text)


@dataclass
class PresidioMasker:
    """Presidio (German spaCy model) plus regexes for IBAN, phone, e-mail and register
    numbers. Implemented in Wave 1 (masking slice); `presidio-analyzer`,
    `presidio-anonymizer` and the spaCy model are not installed yet."""

    language: str = "de"
    spacy_model: str = "de_core_news_lg"
    entities: tuple[EntityKind, ...] = ENTITY_KINDS
    score_threshold: float = 0.5
    allow_list: tuple[str, ...] = ()

    def mask(self, text: str) -> MaskedText:
        raise NotImplementedError("Phase 9 Wave 1: PresidioMasker.mask")


@dataclass
class MaskingBoundary:
    """The one place outbound text is masked. `masker=None` means masking is off and text
    passes unchanged (the pre-Phase 9 behaviour, and the default until 9.A's gate)."""

    masker: Masker | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def enabled(self) -> bool:
        return self.masker is not None

    def outbound(self, text: str) -> str:
        """The text to send: masked when enabled (a `MaskedText`), else `text` itself.

        Raises MaskingError if the masker fails; callers turn that into a failed result
        without sending anything. Text that is already a `MaskedText` is not masked twice.
        """
        masker = self.masker
        if masker is None or isinstance(text, MaskedText):
            return text
        try:
            masked = masker.mask(text)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any masker fault blocks the send
            raise MaskingError(f"masking failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(masked, MaskedText):
            raise MaskingError("masker returned an unmarked string")
        return masked

    def require_masked(self, *values: Any) -> None:
        """Refuse (UnmaskedPayload) any `str` among `values` that did not pass `outbound`,
        while masking is enabled. Strict fake clients call this on every payload."""
        if not self.enabled:
            return
        for value in values:
            if isinstance(value, str) and not isinstance(value, MaskedText):
                raise UnmaskedPayload("unmasked text reached a provider client")
            if isinstance(value, list | tuple):
                self.require_masked(*value)

    def langfuse_mask(self, *, data: Any, **_: Any) -> Any:
        """Langfuse's `mask=` hook (`langfuse.types.MaskFunction`): `data` with every string
        inside it masked, recursively through dicts and lists; returned JSON-serialisable.
        Masking off: `data` unchanged. Implemented in Wave 1 (masking slice)."""
        if not self.enabled:
            return data
        raise NotImplementedError("Phase 9 Wave 1: MaskingBoundary.langfuse_mask")


_BOUNDARY = MaskingBoundary()


def boundary() -> MaskingBoundary:
    """The process-wide boundary every outbound call uses."""
    return _BOUNDARY


def configure(masker: Masker | None) -> MaskingBoundary:
    """Set (or clear, with None) the process-wide masker; returns the boundary."""
    with _BOUNDARY._lock:
        _BOUNDARY.masker = masker
    return _BOUNDARY


def masker_from_config(config: Mapping[str, Any]) -> Masker | None:
    """The masker `privacy.masking` in configs/app.yaml asks for; None when disabled."""
    block = (config.get("privacy") or {}).get("masking") or {}
    if not block.get("enabled", False):
        return None
    return PresidioMasker(
        language=str(block.get("language", "de")),
        spacy_model=str(block.get("spacy_model", "de_core_news_lg")),
        entities=tuple(block.get("entities") or ENTITY_KINDS),
        score_threshold=float(block.get("score_threshold", 0.5)),
        allow_list=tuple(block.get("allow_list") or ()),
    )

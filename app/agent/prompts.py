"""The agent's question text: fetched from Langfuse, with the repo copy as fallback.

The Decisions API takes `instructions` per question rather than one prose
system prompt, so what is versioned here are three templates: the void
question, the per-chunk basis question, and the explain prompt that generates
the German reason.

Langfuse prompt management is the source of truth once seeded, so wording can be
revised and labelled without a deploy. The files in `app/agent/prompts/` are the
fallback for unit tests, CI without keys, and an unreachable Langfuse - and the
text `seed()` pushes on first use.

Whichever source wins, `fingerprint()` hashes the text actually in use, so
revised wording changes the agent version and never serves a cached verdict.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from core import langfuse_client
from core.logging import get_logger
from core.paths import ROOT
from core.settings import load_settings

log = get_logger("agent.prompts")

PROMPT_DIR = ROOT / "app" / "agent" / "prompts"

# role -> prompt name. The names are also the Langfuse prompt names.
PROMPT_NAMES = {
    "void": "void-question",
    "basis": "basis-question",
    "explain": "explain",
}

# The Decisions API restricts question keys to this shape, up to 64 characters.
QUESTION_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

REASON_MAX_CHARS = 600


@dataclass(frozen=True)
class PromptText:
    name: str
    text: str
    source: str  # "langfuse" | "local"
    version: int | None = None


_cache: dict[tuple[str, str], PromptText] = {}


def clear_cache() -> None:
    _cache.clear()


def valid_question_key(key: str) -> bool:
    """Whether a chunk id can be used as a Decisions question key."""
    return bool(QUESTION_KEY.match(key))


def _local(name: str) -> str:
    return (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


def _langfuse(name: str, label: str) -> PromptText:
    """One fetch. Raises on any problem so the caller can fall back."""
    prompt = langfuse_client.client().get_prompt(name, label=label, type="text")
    return PromptText(name=name, text=prompt.prompt, source="langfuse", version=prompt.version)


def load(name: str, label: str = "production") -> PromptText:
    """The text in use, cached per process.

    Falls back to the repo file when Langfuse keys are absent or the fetch
    fails - a missing prompt server must not stop a clause being judged, and
    tests must run offline.
    """
    key = (name, label)
    if key in _cache:
        return _cache[key]

    settings = load_settings()
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        try:
            loaded = _langfuse(name, label)
            log.info("prompt %s v%s from Langfuse (%s)", name, loaded.version, label)
            _cache[key] = loaded
            return loaded
        except Exception as exc:  # noqa: BLE001 - any failure means "use the file"
            log.warning("prompt %s: Langfuse fetch failed (%s); using local file", name, exc)

    loaded = PromptText(name=name, text=_local(name), source="local")
    log.info("prompt %s from local file", name)
    _cache[key] = loaded
    return loaded


def fingerprint(label: str = "production") -> dict[str, str]:
    """Short hash per role of the text actually in use. Feeds the agent version."""
    return {
        role: hashlib.sha256(load(name, label).text.encode("utf-8")).hexdigest()[:12]
        for role, name in PROMPT_NAMES.items()
    }


def reason_schema() -> dict[str, Any]:
    """The explain call's reply shape. The only generated text in the agent."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"reason_de": {"type": "string", "maxLength": REASON_MAX_CHARS}},
        "required": ["reason_de"],
    }


def read_reason(text: str) -> str:
    """Pull `reason_de` out of the explain reply without ever raising.

    A missing or malformed reason is cosmetic - the verdict and its legal basis
    are already decided - so this degrades to an empty string rather than
    costing the clause its result.
    """
    if not text:
        return ""
    try:
        return str(json.loads(text).get("reason_de", ""))[:REASON_MAX_CHARS]
    except (ValueError, AttributeError):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return ""
        try:
            return str(json.loads(text[start : end + 1]).get("reason_de", ""))[:REASON_MAX_CHARS]
        except (ValueError, AttributeError):
            return ""


def seed(client: Any, label: str = "production") -> None:
    """Push the repo copies to Langfuse as a new version each, labelled."""
    for name in PROMPT_NAMES.values():
        client.create_prompt(name=name, prompt=_local(name), labels=[label], type="text")
        log.info("seeded prompt %s with label %s", name, label)

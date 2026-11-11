"""Phase 3 baseline: one clause in, one verdict out. No retrieval, no reasoning loop.

This exists to be beaten. Without a committed number here, "the agent is
better" in Phase 4 is unfalsifiable - so the baseline is deliberately the
simplest thing that could work: a single call, a cheap model, no BGB context.

It lives in `evals/` rather than `app/` because it never ships. The Phase 4
graph will be a different task function scored by the same harness.
"""

import json
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic
from core.logging import get_logger
from core.schemas import Verdict
from pydantic import BaseModel, ValidationError

log = get_logger("evals.baseline")

SYSTEM_PROMPT = """\
Du bist Jurist und prüfst Allgemeine Geschäftsbedingungen (AGB) deutscher \
Online-Shops auf Unwirksamkeit nach deutschem Recht (§§ 305-310 BGB sowie das \
Kaufrecht).

Beurteile die vorgelegte Klausel. Antworte ausschliesslich mit JSON:

{"verdict": "valid" | "potentially_void", "reason_de": "<ein bis zwei Saetze>", \
"confidence": <0.0-1.0>}

- "potentially_void": die Klausel ist nach deutschem AGB-Recht ganz oder \
teilweise unwirksam.
- "valid": die Klausel ist wirksam.

Die Platzhalter <<NAME>>, <<STADT>> und <<STRASSE>> ersetzen anonymisierte \
Angaben und sind kein Mangel der Klausel."""

USER_TEMPLATE = """\
Abschnitt: {title}
Thema: {topics}

Klausel:
{text}"""


class BaselineAnswer(BaseModel):
    """What the model is asked to return. Anything else is a task failure."""

    verdict: Verdict
    reason_de: str = ""
    confidence: float = 0.0


def answer_schema() -> dict[str, Any]:
    """The JSON schema the API enforces on the reply.

    Derived from BaselineAnswer so the shape asked for and the shape validated
    cannot drift apart.
    """
    schema = BaselineAnswer.model_json_schema()
    # Anthropic wants a closed object with every property required.
    schema["additionalProperties"] = False
    schema["required"] = sorted(schema["properties"])
    return schema


@dataclass(frozen=True)
class BaselineOutput:
    verdict: Verdict | None
    reason_de: str
    confidence: float
    raw: str
    error: str | None = None


def render_user_prompt(clause: dict[str, Any]) -> str:
    return USER_TEMPLATE.format(
        title=clause.get("title") or "(ohne Überschrift)",
        topics=", ".join(clause.get("topics") or []) or "(ohne Thema)",
        text=clause["text"],
    )


def parse_answer(content: str) -> BaselineOutput:
    """Parse the model's JSON, treating any failure as a null verdict.

    A null verdict is scored as "not void", so an unparseable answer costs
    recall instead of quietly leaving the item out of the denominator.
    """
    try:
        answer = BaselineAnswer.model_validate_json(content)
    except ValidationError as exc:
        # The model occasionally wraps JSON in prose; try once to recover it.
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            return BaselineOutput(
                None, "", 0.0, content, f"unparseable: {exc.error_count()} errors"
            )
        try:
            answer = BaselineAnswer.model_validate_json(content[start : end + 1])
        except ValidationError as inner:
            return BaselineOutput(
                None, "", 0.0, content, f"unparseable: {inner.error_count()} errors"
            )

    return BaselineOutput(answer.verdict, answer.reason_de, answer.confidence, content)


def classify(clause: dict[str, Any], client: Anthropic, model: dict[str, Any]) -> BaselineOutput:
    try:
        response = client.messages.create(
            model=model["model"],
            max_tokens=model["max_output_tokens"],
            # Anthropic takes the system prompt as its own parameter rather
            # than as a message.
            system=SYSTEM_PROMPT,
            # The API rejects a reply that does not match the schema, so a
            # malformed verdict is impossible rather than merely detected.
            # `temperature` is not a parameter of this API version.
            output_config={"format": {"type": "json_schema", "schema": answer_schema()}},
            messages=[{"role": "user", "content": render_user_prompt(clause)}],
        )
    except Exception as exc:  # noqa: BLE001 - a failed call is data, not a crash
        log.warning("classification failed for %s: %s", clause.get("clause_id"), exc)
        return BaselineOutput(None, "", 0.0, "", f"{type(exc).__name__}: {exc}")

    text = "".join(block.text for block in response.content if block.type == "text")
    return parse_answer(text)


def task(client: Anthropic, model: dict[str, Any]):
    """Build the task function langfuse.run_experiment calls per item."""

    def run(*, item: Any, **_: Any) -> dict[str, Any]:
        clause = item["input"] if isinstance(item, dict) else item.input
        output = classify(dict(clause), client, model)
        return {
            "verdict": output.verdict,
            "reason_de": output.reason_de,
            "confidence": output.confidence,
            "error": output.error,
        }

    return run


def prompt_fingerprint() -> str:
    """Identifies the prompt in a run's metadata.

    Phase 4 moves prompts into Langfuse and versions them properly; until then
    a hash is what makes two runs distinguishable after the fact.
    """
    import hashlib

    payload = json.dumps([SYSTEM_PROMPT, USER_TEMPLATE], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]

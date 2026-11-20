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

from core.budget import RequestBudget
from core.llm import complete
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
    # Both providers want a closed object with every property required.
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
    input_tokens: int = 0
    output_tokens: int = 0


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


def classify(
    clause: dict[str, Any],
    model: dict[str, Any],
    budget: RequestBudget | None = None,
    client: Any = None,
) -> BaselineOutput:
    """One call through the provider layer, so the baseline runs on any provider.

    The v1 comparison needs both sides on the same provider; routing through
    `core.llm.complete` is what lets configs/evals.yaml decide which.
    """
    answer = complete(
        system=SYSTEM_PROMPT,
        user=render_user_prompt(clause),
        schema=answer_schema(),
        model=model,
        budget=budget,
        client=client,
    )
    if answer.error:
        log.warning("classification failed for %s: %s", clause.get("clause_id"), answer.error)
        return BaselineOutput(None, "", 0.0, answer.text, answer.error)

    parsed = parse_answer(answer.text)
    return BaselineOutput(
        parsed.verdict,
        parsed.reason_de,
        parsed.confidence,
        parsed.raw,
        parsed.error,
        answer.input_tokens,
        answer.output_tokens,
    )


def task(model: dict[str, Any], budget: RequestBudget | None = None, client: Any = None):
    """Build the task function langfuse.run_experiment calls per item."""

    def run(*, item: Any, **_: Any) -> dict[str, Any]:
        clause = item["input"] if isinstance(item, dict) else item.input
        output = classify(dict(clause), model, budget, client)
        return {
            "verdict": output.verdict,
            # The baseline has no retrieval, so it cites nothing and grounding
            # does not apply to it. Empty lists keep the contract uniform.
            "legal_basis": [],
            "candidates": [],
            "reason_de": output.reason_de,
            "confidence": output.confidence,
            "error": output.error,
            "input_tokens": output.input_tokens,
            "output_tokens": output.output_tokens,
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

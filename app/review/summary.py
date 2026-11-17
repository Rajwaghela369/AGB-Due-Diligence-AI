"""Build the review summary from durable findings and lawyer decisions.

The deterministic portion is also the cache key for the optional recommendation model.
Keeping that boundary explicit prevents timestamps and model prose from invalidating a useful
cached paragraph, while still making every visible legal decision part of the key.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from core.llm import complete
from core.logging import get_logger

from app.domain import (
    SUMMARY_NO_BASIS,
    Finding,
    ReviewSummary,
    SummaryCounts,
    SummaryFinding,
    SummaryGroup,
    decided_verdict,
)
from app.review.deliverable import deliverable_settings
from app.review.interfaces import SummaryModel

if TYPE_CHECKING:
    from app.review.service import Service

_RECOMMENDATION_SCHEMA = {
    "type": "object",
    "properties": {"recommendation": {"type": "string"}},
    "required": ["recommendation"],
    "additionalProperties": False,
}
_SUMMARY_SYSTEM = (
    "Du formulierst genau einen knappen deutschen Absatz für eine AGB-Prüfung. "
    "Nutze ausschließlich die übergebenen strukturierten Fakten, erfinde keine Normen und "
    "gib keine Rechtsberatung. Antworte als JSON mit dem Feld recommendation."
)
_RECOMMENDATION_MAX_CHARS = 1200
_FAILURE_TTL_SECONDS = 60.0
_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"<[^<>\n]{1,200}>|[*_`#>]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_failure_until: dict[tuple[str, str], float] = {}
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()
log = get_logger("review.summary")


def _now() -> datetime:
    return datetime.now(UTC)


def _excerpt(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    if limit <= 1:
        return "…"[:limit]
    prefix = compact[: limit - 1]
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + "…"


def _in_force(finding: Finding) -> tuple[str | None, list[str]]:
    if finding.verdict is None:
        return None, []
    if finding.decision is None:
        return finding.verdict.verdict, list(finding.verdict.legal_basis)
    return decided_verdict(finding.decision)


def _summary_finding(service: Service, finding: Finding) -> SummaryFinding | None:
    verdict, basis = _in_force(finding)
    model = finding.verdict
    if verdict is None or model is None:
        return None
    statutes = service.statutes or {}
    citations = [statutes[chunk_id].citation for chunk_id in basis if chunk_id in statutes]
    decision = finding.decision
    return SummaryFinding(
        position=finding.position,
        number=finding.clause.number,
        heading=finding.clause.heading,
        excerpt=_excerpt(finding.clause.text, service.deliverable.summary.excerpt_chars),
        verdict=verdict,
        basis=basis,
        citations=citations,
        model_verdict=model.verdict,
        confidence=model.confidence,
        finding_status=finding.finding_status,
        overridden_to_void=(
            finding.finding_status == "overridden"
            and model.verdict != "potentially_void"
            and verdict == "potentially_void"
        ),
        decided_by=decision.decided_by if decision else None,
        note=decision.note if decision else "",
    )


def _group_key(service: Service, finding: SummaryFinding) -> tuple[str, str]:
    statutes = service.statutes or {}
    chunk = statutes.get(finding.basis[0]) if finding.basis else None
    if chunk is None:
        return SUMMARY_NO_BASIS, SUMMARY_NO_BASIS
    number = re.sub(r"[^0-9a-z]+", "-", chunk.paragraph.lower()).strip("-")
    return f"{chunk.law.lower()}-{number}", f"{chunk.paragraph} {chunk.law}"


def _rule_recommendation(counts: SummaryCounts, groups: list[SummaryGroup]) -> str:
    if counts.findings == 0:
        return "Es liegen noch keine prüfbaren Feststellungen vor."
    if counts.potentially_void == 0 and counts.uncertain == 0:
        return (
            f"Von {counts.findings} geprüften Klauseln ist nach den erfassten Entscheidungen "
            "keine als potenziell unwirksam oder klärungsbedürftig eingestuft."
        )
    parts = []
    if counts.potentially_void:
        parts.append(
            f"{counts.potentially_void} "
            + ("Klausel sollte" if counts.potentially_void == 1 else "Klauseln sollten")
            + " überarbeitet oder entfernt werden"
        )
    if counts.uncertain:
        parts.append(
            f"{counts.uncertain} "
            + ("Klausel benötigt" if counts.uncertain == 1 else "Klauseln benötigen")
            + " eine weitere rechtliche Prüfung"
        )
    recommendation = "; ".join(parts) + "."
    labels = [group.label for group in groups if group.key != SUMMARY_NO_BASIS]
    if labels:
        recommendation += " Schwerpunkte sind " + ", ".join(labels[:3]) + "."
    if counts.open_void + counts.open_review:
        recommendation += " Vor dem finalen Export sind die offenen Feststellungen zu entscheiden."
    return recommendation


def _model_text(result: Any) -> str | None:
    if getattr(result, "error", None) or not getattr(result, "text", "").strip():
        return None
    raw = result.text.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict) and isinstance(value.get("recommendation"), str):
        text = _MARKDOWN_LINK.sub(r"\1", value["recommendation"])
        text = _MARKUP.sub("", text)
        text = " ".join(_CONTROL.sub("", text).split())[:_RECOMMENDATION_MAX_CHARS]
        return text or None
    return None


def _model_lock(key: tuple[str, str]) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _cached_or_generate(
    service: Service, decision_hash: str, model_name: str, canonical: str
) -> str | None:
    """Single-flight one decision set and briefly suppress repeated failed calls."""
    key = (decision_hash, model_name)
    with _model_lock(key):
        cached = service.store.caches.get_summary_text(decision_hash, model_name)
        if cached is not None:
            return cached
        now = time.monotonic()
        if _failure_until.get(key, 0.0) > now:
            return None
        try:
            generated = _model_text(service.summary_model(_SUMMARY_SYSTEM, canonical))
        except Exception:  # noqa: BLE001 - the deterministic summary is always available
            log.warning("summary recommendation model failed", exc_info=True)
            generated = None
        if not generated:
            _failure_until[key] = now + _FAILURE_TTL_SECONDS
            return None
        _failure_until.pop(key, None)
        service.store.caches.put_summary_text(decision_hash, model_name, generated)
        return service.store.caches.get_summary_text(decision_hash, model_name)


def _build_summary(service: Service, review_id: str, *, allow_model_call: bool) -> ReviewSummary:
    review = service.get(review_id)
    if review is None:
        raise KeyError(review_id)
    document = service.store.documents.get(review.document_id)
    if document is None:
        raise KeyError(review.document_id)
    findings = service.findings(review_id)

    counts = SummaryCounts()
    visible: list[SummaryFinding] = []
    reviewers: set[str] = set()
    decided_at = []
    for finding in findings:
        if finding.status == "failed":
            counts.failed += 1
        if finding.status == "out_of_scope":
            counts.out_of_scope += 1
        if finding.verdict is None:
            continue
        counts.findings += 1
        if finding.decision is None:
            counts.open += 1
            if finding.verdict.verdict == "potentially_void":
                counts.open_void += 1
            elif finding.verdict.verdict == "uncertain":
                counts.open_review += 1
        else:
            setattr(counts, finding.finding_status, getattr(counts, finding.finding_status) + 1)
            if finding.decision.decided_by:
                reviewers.add(finding.decision.decided_by)
            if finding.decision.decided_at:
                decided_at.append(finding.decision.decided_at)
        item = _summary_finding(service, finding)
        if item is not None:
            setattr(counts, item.verdict, getattr(counts, item.verdict) + 1)
            visible.append(item)

    grouped: dict[tuple[str, str], list[SummaryFinding]] = {}
    for item in visible:
        if item.verdict == "potentially_void":
            grouped.setdefault(_group_key(service, item), []).append(item)
    groups = [
        SummaryGroup(key=key, label=label, findings=items)
        for (key, label), items in grouped.items()
    ]
    groups.sort(key=lambda group: (-len(group.findings), group.key))

    def risk_key(item: SummaryFinding) -> tuple[int, float, int]:
        confirmed = item.verdict == "potentially_void" and (
            item.finding_status == "accepted" or item.overridden_to_void
        )
        rank = 0 if confirmed else 1 if item.verdict == "potentially_void" else 2
        return rank, -item.confidence, item.position

    risks = sorted(
        (item for item in visible if item.verdict in ("potentially_void", "uncertain")),
        key=risk_key,
    )[: service.deliverable.summary.top_risks]
    deterministic = {
        "agent_version": review.agent_version,
        "counts": counts.model_dump(mode="json"),
        "void_groups": [group.model_dump(mode="json") for group in groups],
        "top_risks": [item.model_dump(mode="json") for item in risks],
        "findings": [
            {
                "position": finding.position,
                "status": finding.status,
                "verdict": (
                    finding.verdict.model_dump(mode="json") if finding.verdict is not None else None
                ),
                "decision": (
                    finding.decision.model_dump(mode="json")
                    if finding.decision is not None
                    else None
                ),
            }
            for finding in findings
        ],
    }
    canonical = json.dumps(deterministic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    decision_hash = hashlib.sha256(canonical.encode()).hexdigest()
    recommendation = _rule_recommendation(counts, groups)
    from_llm = False
    settings = service.deliverable.summary
    model_name = str(settings.model.get("name", ""))
    if settings.llm_enabled and service.summary_model is not None:
        cached = service.store.caches.get_summary_text(decision_hash, model_name)
        if cached is None and allow_model_call:
            cached = _cached_or_generate(service, decision_hash, model_name, canonical)
        if cached:
            recommendation = cached
            from_llm = True

    return ReviewSummary(
        review_id=review.review_id,
        document_id=review.document_id,
        filename=document.filename,
        agent_version=review.agent_version,
        review_status=review.status,
        draft=(
            counts.open_void + counts.open_review > 0
            or review.status not in ("finished", "awaiting_decisions")
        ),
        counts=counts,
        void_groups=groups,
        top_risks=risks,
        recommendation=recommendation,
        recommendation_from_llm=from_llm,
        decision_set_hash=decision_hash,
        reviewers=sorted(reviewers),
        last_decided_at=max(decided_at) if decided_at else None,
        generated_at=_now(),
    )


def build_summary(service: Service, review_id: str) -> ReviewSummary:
    """Return the current summary, allowing its single optional cached model call."""
    return _build_summary(service, review_id, allow_model_call=True)


def make_summary_model(
    config: dict[str, Any], *, limiter: Any, client: Any = None
) -> SummaryModel | None:
    """Build the configured completion call, or no callable while the feature is disabled."""
    settings = deliverable_settings(config).summary
    if not settings.llm_enabled:
        return None

    def generate(system: str, user: str) -> Any:
        return complete(
            system,
            user,
            _RECOMMENDATION_SCHEMA,
            settings.model,
            budget=limiter,
            client=client,
        )

    return generate

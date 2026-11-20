"""Publish opted-in lawyer overrides as repeatable evaluation inputs.

The dataset is disabled by default because contract text must not leave the service before
Phase 9 masking. Deterministic item ids make a later correction update the same example.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core import masking
from core.hashing import content_hash
from core.logging import get_logger
from core.masking import boundary

from app.domain import ClauseSpan, ClauseVerdict, Decision, Document, Review

log = get_logger("review.corrections")


def correction_id(content_hash_value: str, review_id: str) -> str:
    payload = f"{content_hash_value}\x1f{review_id}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class CorrectionSettings:
    enabled: bool = False
    dataset: str = "agb-lawyer-corrections"
    allowed_sources: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> CorrectionSettings:
        block = config.get("corrections", {})
        return cls(
            enabled=bool(block.get("enabled", False)),
            dataset=str(block.get("dataset", "agb-lawyer-corrections")),
            allowed_sources=frozenset(str(value) for value in block.get("allowed_sources", [])),
        )


class Corrections:
    """Best-effort Langfuse dataset writer guarded by source and configuration."""

    def __init__(self, langfuse: Any, settings: CorrectionSettings) -> None:
        self.langfuse = langfuse
        self.settings = settings

    def upsert(
        self,
        document: Document,
        review: Review,
        clause: ClauseSpan,
        verdict: ClauseVerdict,
        decision: Decision,
        *,
        previous_action: str | None = None,
    ) -> None:
        if (
            not self.settings.enabled
            or self.langfuse is None
            or document.source_type not in self.settings.allowed_sources
        ):
            return
        item_id = correction_id(clause.content_hash, review.review_id)
        if decision.action != "override":
            if previous_action != "override":
                return
            try:
                self.langfuse.create_dataset_item(
                    id=item_id,
                    dataset_name=self.settings.dataset,
                    status="ARCHIVED",
                )
            except Exception:  # noqa: BLE001 - dataset publishing cannot reject a decision
                log.warning("could not archive correction %s", item_id, exc_info=True)
            return
        try:
            self.langfuse.create_dataset(
                name=self.settings.dataset,
                description="Lawyer overrides approved for continuous evaluation.",
            )
            # Dataset items are not observations, so the `mask=` hook does not see them; they
            # are masked here (9.A), in one scope of this review primed with the clause, so
            # the title and the text share placeholders. A masking failure raises before
            # anything is sent.
            mask = boundary().langfuse_mask
            with masking.scope(
                review.owner_id, review.review_id, prime=[clause.text, clause.heading]
            ):
                item_input = mask(
                    data={
                        "clause_id": clause.content_hash,
                        "title": clause.heading or clause.number or "",
                        "text": clause.text,
                        "topics": [clause.topic] if clause.topic else [],
                    }
                )
                expected = mask(
                    data={
                        "model_verdict": verdict.verdict,
                        "corrected_verdict": decision.corrected_verdict,
                        "corrected_basis": list(decision.corrected_basis or decision.model_basis),
                    }
                )
                metadata = mask(
                    data={
                        "agent_version": review.agent_version,
                        "review_id": review.review_id,
                        "review_link": f"/reviews/{review.review_id}",
                        "content_hash": clause.content_hash,
                    }
                )
            self.langfuse.create_dataset_item(
                id=item_id,
                dataset_name=self.settings.dataset,
                input=item_input,
                expected_output=expected,
                metadata=metadata,
            )
        except Exception:  # noqa: BLE001 - dataset publishing cannot reject a decision
            log.warning("could not upsert correction %s", item_id, exc_info=True)


def evaluate(*, approved: bool) -> int:
    """Run the current agent over every correction after explicit paid-run approval."""
    if not approved:
        raise SystemExit("Refusing paid correction evaluation without --yes")

    from core import langfuse_client
    from core.config import load_yaml
    from core.paths import CONFIGS

    from app.agent.run import make_judge
    from app.review.limiter import build_limiter

    config = load_yaml(CONFIGS / "app.yaml")
    # Phase 9 (9.A): mask this run as the app would; the items are masked already, but the
    # traces of the new verdicts carry the model's answers.
    masker = masking.masker_from_config(config)
    if masker is not None:
        masker.load()  # type: ignore[attr-defined]
    masking.configure(masker)
    settings = CorrectionSettings.from_config(config)
    client = langfuse_client.client()
    items = list(client.get_dataset(settings.dataset).items)
    print(f"Correction clauses: {len(items)}")
    limiter = build_limiter(config)
    judge = make_judge(config, limiter=limiter)

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        payload = dict(item["input"] if isinstance(item, dict) else item.input or {})
        text = str(payload["text"])
        verdict = judge(
            ClauseSpan(
                position=0,
                heading=payload.get("title") or None,
                text=text,
                content_hash=str(payload.get("clause_id") or content_hash(text)),
            )
        )
        return verdict.model_dump(mode="json")

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    client.run_experiment(
        name="lawyer-corrections",
        run_name=f"corrections-{stamp}",
        description=f"Current agent over {len(items)} lawyer corrections",
        data=items,
        task=task,
        max_concurrency=int(config.get("review", {}).get("max_concurrency", 4)),
        metadata={"agent_version": judge.agent_version, "items": str(len(items))},
    )
    return len(items)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the lawyer corrections dataset")
    parser.add_argument("--yes", action="store_true", help="approve this paid Gemini run")
    args = parser.parse_args()
    evaluate(approved=args.yes)


if __name__ == "__main__":
    main()

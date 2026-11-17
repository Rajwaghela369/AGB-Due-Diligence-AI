"""Turn lawyer feedback into durable rows and trace scores.

Feedback is resolved through the stored review rather than trusting client-supplied
trace identifiers.  Stable feedback and score ids make a later edit an upsert in both
our database and Langfuse.
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.logging import get_logger

from app.domain import REASON_STEP, Decision, Feedback
from app.store.interfaces import Store

log = get_logger("review.feedback")


def _stable_id(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class FeedbackService:
    """Persist feedback and attach it to the observations that produced a verdict."""

    def __init__(self, store: Store, langfuse: Any) -> None:
        self.store = store
        self.langfuse = langfuse

    def submit(
        self,
        request: Any,
        *,
        author: str | None = None,
        source: str = "vote",
    ) -> tuple[Feedback, list[str]]:
        if request.vote == "down" and not request.reasons:
            raise ValueError("downvotes require at least one reason")
        if "verdict_wrong" in request.reasons and request.corrected_verdict is None:
            raise ValueError("verdict_wrong requires corrected_verdict")
        if "basis_wrong" in request.reasons and not request.corrected_basis:
            raise ValueError("basis_wrong requires corrected_basis")
        review, verdict = self._verdict(request.review_id, request.position)
        identity = author or "anonymous"
        feedback_id = _stable_id(request.review_id, request.position, identity)

        steps: list[str]
        if request.vote == "up":
            steps = ["clause_span"]
        else:
            steps = list(
                dict.fromkeys(
                    step for reason in request.reasons if (step := REASON_STEP[reason]) is not None
                )
            )

        # By id, not "latest for the clause": a newer decision-derived row has its own id.
        previous = self.store.feedback.get(feedback_id)
        if previous is not None:
            for step in previous.observations.keys() - set(steps):
                self._delete_score(feedback_id, step)

        observations: dict[str, str | None] = {}
        scored_steps: list[str] = []
        score_comment = self._score_comment(request)
        for step in steps:
            observation_id = getattr(verdict.observations, step)
            observations[step] = observation_id
            if observation_id is None or self.langfuse is None:
                continue
            try:
                self.langfuse.create_score(
                    name=f"feedback.{step}",
                    value=1 if request.vote == "up" else -1,
                    trace_id=verdict.trace_id,
                    observation_id=observation_id,
                    score_id=_stable_id(feedback_id, step),
                    data_type="NUMERIC",
                    comment=score_comment,
                )
            except Exception:  # noqa: BLE001 - tracing cannot reject durable feedback
                log.warning("could not record Langfuse feedback score", exc_info=True)
            else:
                scored_steps.append(step)

        feedback = Feedback(
            feedback_id=feedback_id,
            review_id=request.review_id,
            position=request.position,
            agent_version=review.agent_version,
            vote=request.vote,
            reasons=request.reasons,
            corrected_verdict=request.corrected_verdict,
            corrected_basis=request.corrected_basis,
            comment=request.comment,
            source=source,
            observations=observations,
        )
        return self.store.feedback.put(feedback), scored_steps

    def _delete_score(self, feedback_id: str, step: str) -> None:
        """Remove a score made obsolete by an edited vote without rejecting the edit."""
        if self.langfuse is None:
            return
        score_id = _stable_id(feedback_id, step)
        try:
            self.langfuse.api.scores.delete(score_id)
        except Exception:  # noqa: BLE001 - tracing is best effort by contract
            log.warning("could not delete stale Langfuse score %s", score_id, exc_info=True)

    def decide(
        self,
        review_id: str,
        position: int,
        request: Any,
        *,
        decided_by: str | None = None,
        request_id: str | None = None,
    ) -> Decision:
        """Phase 5 decision path, kept working until Phase 7 Wave 1 moves the route to
        `ReviewService.decide` (which also maintains counts, events and resumes)."""
        if request.action == "override" and request.corrected_verdict is None:
            raise ValueError("override requires corrected_verdict")
        review, verdict = self._verdict(review_id, position)
        previous = self.store.decisions.for_review(review_id).get(position)
        decision = self.store.decisions.put(
            Decision(
                review_id=review_id,
                position=position,
                action=request.action,
                corrected_verdict=request.corrected_verdict,
                corrected_basis=request.corrected_basis,
                note=request.note,
                decided_by=decided_by,
                model_verdict=verdict.verdict,
                model_basis=verdict.legal_basis,
            ),
            agent_version=review.agent_version,
            request_id=request_id,
        )
        self.record_decision(decision, previous_action=previous.action if previous else None)
        return decision

    def record_decision(self, decision: Decision, *, previous_action: str | None = None) -> None:
        """Score a stored decision through the same stable feedback identity.

        Dismissal is deliberately its own score: it says the finding is irrelevant, not
        that the model verdict was wrong. Moving between actions removes scores that no
        longer describe the current decision.
        """
        _review, verdict = self._verdict(decision.review_id, decision.position)
        feedback_id = _stable_id(decision.review_id, decision.position, "decision")
        dismissed_id = _stable_id(feedback_id, "dismissed")
        if decision.action == "dismiss":
            previous = self.store.feedback.get(feedback_id)
            if previous is not None:
                for step in previous.observations:
                    self._delete_score(feedback_id, step)
                self.store.feedback.delete(feedback_id)
            if self.langfuse is not None and verdict.observations.clause_span is not None:
                try:
                    self.langfuse.create_score(
                        name="dismissed",
                        value=1,
                        trace_id=verdict.trace_id,
                        observation_id=verdict.observations.clause_span,
                        score_id=dismissed_id,
                        data_type="NUMERIC",
                        comment=decision.note,
                    )
                except Exception:  # noqa: BLE001 - tracing cannot reject a decision
                    log.warning("could not record Langfuse dismissal score", exc_info=True)
            return

        if previous_action == "dismiss":
            self._delete_score_id(dismissed_id)
        if decision.action == "accept":
            request = _FeedbackRequest(
                decision.review_id, decision.position, "up", [], None, [], decision.note
            )
        else:
            corrected_basis = decision.corrected_basis or decision.model_basis
            reasons = ["verdict_wrong"]
            if set(corrected_basis) != set(decision.model_basis):
                reasons.append("basis_wrong")
            request = _FeedbackRequest(
                decision.review_id,
                decision.position,
                "down",
                reasons,
                decision.corrected_verdict,
                corrected_basis if "basis_wrong" in reasons else [],
                decision.note,
            )
        self.submit(request, author="decision", source="decision")

    def _delete_score_id(self, score_id: str) -> None:
        if self.langfuse is None:
            return
        try:
            self.langfuse.api.scores.delete(score_id)
        except Exception:  # noqa: BLE001 - tracing is best effort by contract
            log.warning("could not delete stale Langfuse score %s", score_id, exc_info=True)

    def _verdict(self, review_id: str, position: int):
        review = self.store.reviews.get(review_id)
        if review is None:
            raise LookupError("review not found")
        clause = next(
            (
                clause
                for clause in self.store.documents.clauses(review.document_id)
                if clause.position == position
            ),
            None,
        )
        if clause is None:
            raise LookupError("clause not found")
        verdict = self.store.verdicts.get(clause.content_hash, review.agent_version)
        if verdict is None:
            raise LookupError("verdict not found")
        return review, verdict

    @staticmethod
    def _score_comment(request: Any) -> str:
        corrections = []
        if request.corrected_verdict is not None:
            corrections.append(f"Korrigiertes Urteil: {request.corrected_verdict}")
        if request.corrected_basis:
            corrections.append(f"Korrigierte Rechtsgrundlage: {', '.join(request.corrected_basis)}")
        return "\n".join(part for part in [request.comment, *corrections] if part)


class _FeedbackRequest:
    """Internal request shape used when a decision also records feedback."""

    def __init__(
        self,
        review_id: str,
        position: int,
        vote: str,
        reasons: list,
        corrected_verdict: str | None,
        corrected_basis: list[str],
        comment: str,
    ) -> None:
        self.review_id = review_id
        self.position = position
        self.vote = vote
        self.reasons = reasons
        self.corrected_verdict = corrected_verdict
        self.corrected_basis = corrected_basis
        self.comment = comment

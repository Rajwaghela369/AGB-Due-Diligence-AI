"""Turn lawyer feedback into durable rows and trace scores.

Feedback is resolved through the stored review rather than trusting client-supplied
trace identifiers.  Stable feedback and score ids make a later edit an upsert in both
our database and Langfuse.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.domain import REASON_STEP, Decision, Feedback
from app.store.interfaces import Store


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

        observations: dict[str, str | None] = {}
        scored_steps: list[str] = []
        score_comment = self._score_comment(request)
        for step in steps:
            observation_id = getattr(verdict.observations, step)
            observations[step] = observation_id
            if observation_id is None or self.langfuse is None:
                continue
            self.langfuse.create_score(
                name=f"feedback.{step}",
                value=1 if request.vote == "up" else -1,
                trace_id=verdict.trace_id,
                observation_id=observation_id,
                score_id=_stable_id(feedback_id, step),
                data_type="NUMERIC",
                comment=score_comment,
            )
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

    def decide(self, review_id: str, position: int, request: Any) -> Decision:
        if request.action == "override" and request.corrected_verdict is None:
            raise ValueError("override requires corrected_verdict")
        self._verdict(review_id, position)
        decision = self.store.decisions.put(
            Decision(
                review_id=review_id,
                position=position,
                action=request.action,
                corrected_verdict=request.corrected_verdict,
                corrected_basis=request.corrected_basis,
                note=request.note,
            )
        )
        if request.action == "accept":
            feedback = _FeedbackRequest(review_id, position, "up", [], None, [], request.note)
            self.submit(feedback, author="decision", source="decision")
        elif request.action == "override":
            feedback = _FeedbackRequest(
                review_id,
                position,
                "down",
                ["verdict_wrong"],
                request.corrected_verdict,
                request.corrected_basis,
                request.note,
            )
            self.submit(feedback, author="decision", source="decision")
        return decision

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

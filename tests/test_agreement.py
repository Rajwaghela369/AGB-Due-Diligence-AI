"""Agreement is calculated from current decisions and exposed per agent version."""

from core.hashing import content_hash
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, DecisionIn, Document, Review, Reviewer
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.memory import memory_store


class Judge:
    agent_version = "agent-agreement"


def test_seeded_agreement_exact_and_redecision_replaces() -> None:
    store = memory_store()
    clauses = [
        ClauseSpan(position=i, text=f"Klausel {i}", content_hash=content_hash(f"Klausel {i}"))
        for i in range(4)
    ]
    store.documents.save(
        Document(
            document_id="doc",
            filename="terms.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash="text",
        ),
        clauses,
        {clause.content_hash: clause.text for clause in clauses},
    )
    review = store.reviews.create(
        Review(
            review_id="review",
            document_id="doc",
            agent_version=Judge.agent_version,
            scope=["liability"],
            status="finished",
        )
    )
    verdicts = ["potentially_void", "potentially_void", "valid", "uncertain"]
    for clause, verdict in zip(clauses, verdicts, strict=True):
        store.reviews.set_clause_status(review.review_id, clause.position, "done")
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version=review.agent_version,
                verdict=verdict,
                confidence=0.8,
            )
        )
    feedback = FeedbackService(store, None)
    service = build_service(store, Judge(), build_limiter({}), statutes={}, feedback=feedback)
    service.decide("review", 0, DecisionIn(action="accept"), Reviewer())
    service.decide(
        "review",
        1,
        DecisionIn(action="override", corrected_verdict="valid"),
        Reviewer(),
    )
    service.decide("review", 2, DecisionIn(action="dismiss"), Reviewer())
    service.decide("review", 3, DecisionIn(action="accept"), Reviewer())

    agreement = service.agreement()
    assert agreement.model_dump(exclude={"last_decided_at"}) == {
        "agent_version": Judge.agent_version,
        "decided": 4,
        "kept": 2,
        "overridden": 1,
        "dismissed": 1,
        "rate": 2 / 3,
        "by_verdict": {
            "potentially_void": {
                "decided": 2,
                "kept": 1,
                "overridden": 1,
                "dismissed": 0,
                "rate": 0.5,
            },
            "uncertain": {
                "decided": 1,
                "kept": 1,
                "overridden": 0,
                "dismissed": 0,
                "rate": 1.0,
            },
            "valid": {
                "decided": 1,
                "kept": 0,
                "overridden": 0,
                "dismissed": 1,
                "rate": None,
            },
        },
    }

    service.decide("review", 1, DecisionIn(action="accept"), Reviewer())
    updated = service.agreement()
    assert (updated.decided, updated.kept, updated.overridden, updated.rate) == (4, 3, 0, 1.0)
    assert len(store.decisions.history("review", 1)) == 2

    app = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}},
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/metrics/agreement")
        missing = client.get("/api/v1/metrics/agreement?agent_version=older")
    assert response.json()["current"] is True
    assert response.json()["agreement"]["rate"] == 1.0
    assert missing.json()["agreement"]["decided"] == 0
    assert missing.json()["current"] is False

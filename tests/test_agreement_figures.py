"""The public agreement metric must retain exact per-version decision semantics."""

from core.hashing import content_hash
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.domain import ClauseSpan, ClauseVerdict, DecisionIn, Document, Review, Reviewer
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.memory import memory_store


class Judge:
    agent_version = "current-agent"


def seed_review(store, service, version: str, decisions: list[tuple[str, str | None]]) -> str:
    """Seed model verdicts and their current lawyer decisions for one agent version."""
    review_id = f"review-{version}"
    clauses = [
        ClauseSpan(
            position=position,
            text=f"{version} clause {position}",
            content_hash=content_hash(f"{version} clause {position}"),
        )
        for position in range(len(decisions))
    ]
    store.documents.save(
        Document(
            document_id=f"document-{version}",
            filename="terms.pdf",
            source_type="pdf",
            parser_version="p1",
            splitter_version="s1",
            text_hash=f"text-{version}",
        ),
        clauses,
        {clause.content_hash: clause.text for clause in clauses},
    )
    store.reviews.create(
        Review(
            review_id=review_id,
            document_id=f"document-{version}",
            agent_version=version,
            scope=["liability"],
            status="finished",
        )
    )
    for clause, (model_verdict, action) in zip(clauses, decisions, strict=True):
        store.reviews.set_clause_status(review_id, clause.position, "done")
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version=version,
                verdict=model_verdict,
                confidence=0.8,
            )
        )
        if action is not None:
            corrected = "valid" if action == "override" else None
            service.decide(
                review_id,
                clause.position,
                DecisionIn(action=action, corrected_verdict=corrected),
                Reviewer(),
            )
    return review_id


def test_agreement_endpoint_reports_exact_figures_for_two_agent_versions() -> None:
    store = memory_store()
    feedback = FeedbackService(store, None)
    service = build_service(store, Judge(), build_limiter({}), statutes={}, feedback=feedback)
    seed_review(
        store,
        service,
        "agent-v1",
        [
            ("potentially_void", "accept"),
            ("potentially_void", "override"),
            ("valid", "dismiss"),
        ],
    )
    seed_review(
        store,
        service,
        "agent-v2",
        [
            ("valid", "accept"),
            ("uncertain", "override"),
            ("potentially_void", None),
        ],
    )
    app = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=feedback,
        settings={"ingest": {"max_upload_mb": 20}},
    )

    with TestClient(app) as client:
        v1 = client.get("/api/v1/metrics/agreement?agent_version=agent-v1")
        v2 = client.get("/api/v1/metrics/agreement?agent_version=agent-v2")

    assert v1.status_code == v2.status_code == 200
    assert v1.json()["agreement"] == {
        "agent_version": "agent-v1",
        "decided": 3,
        "kept": 1,
        "overridden": 1,
        "dismissed": 1,
        "rate": 0.5,
        "by_verdict": {
            "potentially_void": {
                "decided": 2,
                "kept": 1,
                "overridden": 1,
                "dismissed": 0,
                "rate": 0.5,
            },
            "uncertain": {"decided": 0, "kept": 0, "overridden": 0, "dismissed": 0, "rate": None},
            "valid": {"decided": 1, "kept": 0, "overridden": 0, "dismissed": 1, "rate": None},
        },
        "last_decided_at": v1.json()["agreement"]["last_decided_at"],
    }
    assert v2.json()["agreement"] == {
        "agent_version": "agent-v2",
        "decided": 2,
        "kept": 1,
        "overridden": 1,
        "dismissed": 0,
        "rate": 0.5,
        "by_verdict": {
            "potentially_void": {
                "decided": 0,
                "kept": 0,
                "overridden": 0,
                "dismissed": 0,
                "rate": None,
            },
            "uncertain": {"decided": 1, "kept": 0, "overridden": 1, "dismissed": 0, "rate": 0.0},
            "valid": {"decided": 1, "kept": 1, "overridden": 0, "dismissed": 0, "rate": 1.0},
        },
        "last_decided_at": v2.json()["agreement"]["last_decided_at"],
    }

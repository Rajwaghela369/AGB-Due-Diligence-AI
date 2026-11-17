"""Reference counting must be independent of document shape and deletion order (9.B).

Hypothesis builds random documents whose clause sets overlap (so content-addressed rows are
shared), whose source files sometimes coincide (so blobs are shared), and gives each one a
finished review with clause statuses, events, a decision and feedback. It then deletes the
documents in a random order. After every deletion exactly the rows still referenced by a
remaining document remain, nothing belonging to a deleted document or review is left
behind, and the outbox has erased exactly the blobs no remaining document uses.
"""

from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.domain import (
    ClauseSpan,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Review,
    blob_key,
    default_principal,
)
from app.review.deletion import delete_document, process_erasures
from app.store.memory import memory_store


@st.composite
def document_sets(draw):
    documents = draw(
        st.lists(
            st.tuples(
                st.sets(st.integers(min_value=0, max_value=7), min_size=1, max_size=6),
                st.integers(min_value=0, max_value=3),  # source file: equal → shared blob
            ),
            min_size=1,
            max_size=7,
        )
    )
    order = draw(st.permutations(tuple(range(len(documents)))))
    return documents, order


def _holder(store):
    def ignore(_key: str) -> None:
        return None

    holder = SimpleNamespace(
        store=store,
        tracer=SimpleNamespace(
            api=SimpleNamespace(
                trace=SimpleNamespace(delete=ignore),
                scores=SimpleNamespace(delete=ignore),
                dataset_items=SimpleNamespace(delete=ignore),
            )
        ),
        judge=SimpleNamespace(forget=ignore),
    )
    holder.process_erasures = lambda limit=100: process_erasures(holder, limit)
    return holder


def _seed(store, number: int, values: set[int], source: int) -> None:
    document = Document(
        document_id=f"doc-{number}",
        filename=f"doc-{number}.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash=f"doc-{number}",
        source_hash=f"source-{source}",
    )
    clauses = [
        ClauseSpan(position=position, text=f"clause-{value}", content_hash=f"hash-{value}")
        for position, value in enumerate(sorted(values))
    ]
    store.documents.save(document, clauses, {c.content_hash: c.text for c in clauses})
    store.documents.record_upload(document.document_id, document.filename, "pdf", 3)
    store.files.put(blob_key(document), b"pdf")
    for clause in clauses:
        store.verdicts.save(
            ClauseVerdict(
                content_hash=clause.content_hash,
                agent_version="agent",
                verdict="valid",
                confidence=0.9,
                trace_id=f"verdict-trace-{clause.content_hash}",
            )
        )
        store.caches.put_embedding(clause.content_hash, "embed", [1.0])
        store.caches.put_retrieval(clause.content_hash, "index", "policy", [])
        store.caches.put_topics("router", {clause.content_hash: "other"})

    review_id = f"review-{number}"
    store.reviews.create(
        Review(
            review_id=review_id,
            document_id=document.document_id,
            agent_version="agent",
            scope=["liability"],
            status="finished",
            trace_id=f"review-trace-{number}",
        )
    )
    store.reviews.set_clause_statuses(
        review_id, {clause.position: ("done", None) for clause in clauses}
    )
    store.events.append(review_id, "RUN_FINISHED", {})
    store.decisions.put(
        Decision(review_id=review_id, position=0, action="accept", model_verdict="valid"),
        agent_version="agent",
    )
    store.feedback.put(
        Feedback(
            feedback_id=f"feedback-{number}",
            review_id=review_id,
            position=0,
            agent_version="agent",
            vote="up",
            observations={"judge": "observation"},
        )
    )


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(document_sets())
def test_random_shared_clauses_leave_exactly_the_still_referenced_rows(case) -> None:
    documents, order = case
    store = memory_store()
    for number, (values, source) in enumerate(documents):
        _seed(store, number, values, source)
    holder = _holder(store)

    remaining = set(range(len(documents)))
    for step, number in enumerate(order, start=1):
        report = delete_document(holder, f"doc-{number}", default_principal())
        remaining.remove(number)

        referenced = {f"hash-{value}" for index in remaining for value in documents[index][0]}
        assert set(store.documents._texts) == referenced
        assert set(store.documents._text_created) == referenced
        assert {key[0] for key in store.verdicts._rows} == referenced
        assert {key[0] for key in store.caches._embeddings} == referenced
        assert {key[0] for key in store.caches._retrievals} == referenced
        assert {key[0] for key in store.caches._topics} == referenced
        # every content row of the deleted document is either removed or kept as shared
        assert report.removed["clause_texts"] + report.kept_shared["clause_texts"] == len(
            documents[number][0]
        )

        live_documents = {f"doc-{index}" for index in remaining}
        live_reviews = {f"review-{index}" for index in remaining}
        assert set(store.documents._documents) == live_documents
        assert set(store.documents._clauses) == live_documents
        assert {row["document_id"] for row in store.documents.uploads} == live_documents
        assert set(store.reviews._reviews) == live_reviews
        assert set(store.reviews._clauses) <= live_reviews
        assert set(store.events._events) <= live_reviews
        assert {key[0] for key in store.decisions._rows} <= live_reviews
        assert {event.review_id for event in store.decisions._events} <= live_reviews
        assert {row.review_id for row in store.feedback._rows.values()} <= live_reviews

        # the outbox ran: a blob goes exactly when no remaining document has its source
        live_blobs = {f"source-{documents[index][1]}.pdf" for index in remaining}
        assert set(store.files.keys()) == live_blobs
        assert store.deletion.pending_erasures() == []
        assert len(store.deletion.tombstones()) == step

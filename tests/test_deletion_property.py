"""Reference counting must be independent of document shape and deletion order (9.B).

Hypothesis builds random documents whose clause sets overlap (so content-addressed rows are
shared), whose source files sometimes coincide, also across owners (so blobs are shared),
and gives each one a finished review with clause statuses, an event, a decision (accepted,
dismissed or overridden), feedback, a summary paragraph and live LangGraph checkpoints. It
then deletes the documents in a random order, each by its own owner. After every deletion
exactly the rows still referenced by a remaining document remain, nothing belonging to a
deleted document or review is left behind (summaries and checkpoint threads included), and
the outbox has erased exactly the expected external things: every `(target, key)` is
computed here independently, with the id helpers of the modules that wrote them.
"""

import hashlib
from types import SimpleNamespace

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from langgraph.checkpoint.base import empty_checkpoint

from app.agent.run import erase_threads
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
from app.review.checkpoint import memory_checkpointer
from app.review.corrections import correction_id
from app.review.deletion import delete_document, process_erasures
from app.review.feedback import _stable_id
from app.store.memory import memory_store

ACTIONS = ("accept", "dismiss", "override")


@st.composite
def document_sets(draw):
    documents = draw(
        st.lists(
            st.tuples(
                st.sets(st.integers(min_value=0, max_value=7), min_size=1, max_size=6),
                st.integers(min_value=0, max_value=3),  # source file: equal → shared blob
                st.sampled_from(("anna", "ben")),  # owner: a blob is shared across owners
                st.sampled_from(ACTIONS),  # the decision on clause 0
            ),
            min_size=1,
            max_size=7,
        )
    )
    order = draw(st.permutations(tuple(range(len(documents)))))
    return documents, order


def _holder(store, saver, erased: list[tuple[str, str]]):
    def recorder(target: str):
        return lambda key: erased.append((target, key))

    def forget(key: str) -> int:
        erased.append(("checkpoint", key))
        return erase_threads(saver, key)

    holder = SimpleNamespace(
        store=store,
        tracer=SimpleNamespace(
            api=SimpleNamespace(
                trace=SimpleNamespace(delete=recorder("trace")),
                scores=SimpleNamespace(delete=recorder("score")),
                dataset_items=SimpleNamespace(delete=recorder("dataset_item")),
            )
        ),
        judge=SimpleNamespace(forget=forget),
    )
    files_delete = store.files.delete

    def delete_blob(key: str) -> bool:
        erased.append(("blob", key))
        return files_delete(key)

    store.files.delete = delete_blob
    holder.process_erasures = lambda limit=100: process_erasures(holder, limit)
    return holder


def _checkpoint(saver, thread: str) -> None:
    config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    saver.put(config, empty_checkpoint(), {}, {})


def _seed(store, saver, number: int, values: set[int], source: int, owner: str, action: str):
    document = Document(
        document_id=f"doc-{number}",
        filename=f"doc-{number}.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash=f"doc-{number}",
        source_hash=f"source-{source}",
        owner_id=owner,
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
            owner_id=owner,
        )
    )
    store.reviews.set_clause_statuses(
        review_id, {clause.position: ("done", None) for clause in clauses}
    )
    store.events.append(review_id, "RUN_FINISHED", {})
    store.decisions.put(
        Decision(
            review_id=review_id,
            position=0,
            action=action,
            corrected_verdict="potentially_void" if action == "override" else None,
            model_verdict="valid",
        ),
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
    store.caches.put_summary_text(f"set-{number}", "model", "Absatz.", review_id=review_id)
    for clause in clauses:  # an interrupted clause per position, as the runner names them
        _checkpoint(saver, f"{review_id}:{clause.position}")


def _expected(documents, number: int, remaining: set[int]) -> set[tuple[str, str]]:
    values, source, _owner, action = documents[number]
    review_id = f"review-{number}"
    referenced = {f"hash-{value}" for index in remaining for value in documents[index][0]}
    expected = {
        ("trace", f"review-trace-{number}"),
        ("score", hashlib.sha256(f"lawyer_agreement\x1f{review_id}".encode()).hexdigest()),
        ("score", _stable_id(f"feedback-{number}", "judge")),
        ("checkpoint", f"{review_id}:*"),
    }
    if action == "dismiss":
        expected.add(("score", _stable_id(_stable_id(review_id, 0, "decision"), "dismissed")))
    if action == "override":
        expected.add(("dataset_item", correction_id(f"hash-{min(values)}", review_id)))
    expected |= {
        ("trace", f"verdict-trace-hash-{value}")
        for value in values
        if f"hash-{value}" not in referenced
    }
    if all(documents[index][1] != source for index in remaining):
        expected.add(("blob", f"source-{source}.pdf"))
    return expected


@settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(document_sets())
def test_random_shared_clauses_leave_exactly_the_still_referenced_rows(case) -> None:
    documents, order = case
    store = memory_store()
    saver = memory_checkpointer().saver
    for number, (values, source, owner, action) in enumerate(documents):
        _seed(store, saver, number, values, source, owner, action)
    erased: list[tuple[str, str]] = []
    holder = _holder(store, saver, erased)

    remaining = set(range(len(documents)))
    for step, number in enumerate(order, start=1):
        erased.clear()
        owner = documents[number][2]
        report = delete_document(holder, f"doc-{number}", default_principal(owner))
        remaining.remove(number)

        # the outbox erased exactly what only this document used, each thing once
        expected = _expected(documents, number, remaining)
        assert {(task.target, task.key) for task in report.erasures} == expected
        assert sorted(erased) == sorted(expected)
        assert all(task.done_at is not None for task in report.erasures)

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
        # summaries and checkpoints: exactly those of the remaining reviews
        assert set(store.caches._summary_reviews.values()) == live_reviews
        assert set(store.caches._summaries) == {(f"set-{index}", "model") for index in remaining}
        assert report.removed["summary_texts"] == 1
        assert {thread.split(":")[0] for thread in saver.storage} == live_reviews

        # a blob goes exactly when no remaining document has its source, whoever owns it
        live_blobs = {f"source-{documents[index][1]}.pdf" for index in remaining}
        assert set(store.files.keys()) == live_blobs
        assert store.deletion.pending_erasures() == []
        assert len(store.deletion.tombstones()) == step

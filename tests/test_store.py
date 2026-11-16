"""One repository suite, run against the in-memory fakes and, when `TEST_DATABASE_URL` is set,
against Postgres.

Point `TEST_DATABASE_URL` at a throwaway Neon branch: the fixture applies the migrations
there and every test uses fresh random ids, so tests never collide and need no cleanup.
"""

import os
import threading
import uuid
from contextlib import contextmanager

import pytest
from core.hashing import content_hash

from app.domain import (
    ClauseSpan,
    ClauseVerdict,
    Decision,
    Document,
    Feedback,
    Rect,
    Review,
    ReviewCounts,
    StepObservations,
)
from app.store.memory import memory_store

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@contextmanager
def _connect():
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        yield conn


@pytest.fixture(scope="session")
def _migrated():
    if DATABASE_URL:
        from app.store.migrate import apply_pending

        apply_pending(_connect)


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set"),
        ),
    ]
)
def store(request, tmp_path, _migrated):
    if request.param == "memory":
        return memory_store()
    from app.store.files import LocalFileStore
    from app.store.postgres import postgres_store

    return postgres_store(LocalFileStore(tmp_path), _connect)


def uid() -> str:
    return uuid.uuid4().hex


def make_document(document_id=None, parser="p1", splitter="s1") -> Document:
    return Document(
        document_id=document_id or uid(),
        filename="agb.pdf",
        source_type="pdf",
        page_count=2,
        parser_version=parser,
        splitter_version=splitter,
        text_hash=uid(),
    )


def make_clause(position: int, text: str) -> ClauseSpan:
    return ClauseSpan(
        position=position,
        number=f"{position + 1}.1",
        heading="§ 6 Haftung",
        text=text,
        content_hash=content_hash(text),
        page=1,
        rects=[Rect(page=1, x0=1.0, top=2.0, x1=3.0, bottom=4.0)],
        in_scope=position != 2,
    )


def make_verdict(h: str, version="v1", verdict="valid", reason="Zulässig.") -> ClauseVerdict:
    return ClauseVerdict(
        content_hash=h,
        agent_version=version,
        verdict=verdict,
        legal_basis=["bgb-309-7-a"],
        candidates=["bgb-309-7-a", "bgb-307"],
        basis_scores={"bgb-309-7-a": 0.9},
        reason_de=reason,
        confidence=0.8,
        void_probability=0.2,
        check="threshold",
        trace_id="t1",
        observations=StepObservations(judge="obs-1"),
        input_tokens=10,
        output_tokens=5,
        latency_ms=300,
    )


def make_review(document_id=None, version="v1", status="queued") -> Review:
    return Review(
        review_id=uid(),
        document_id=document_id or uid(),
        agent_version=version,
        scope=["liability", "warranty"],
        status=status,
    )


# --- files --------------------------------------------------------------------


def test_file_put_get_exists_and_put_is_idempotent(store):
    key = f"documents/{uid()}.pdf"

    assert not store.files.exists(key)
    assert store.files.put(key, b"first") == key
    assert store.files.put(key, b"second") == key  # immutable: the first write stands
    assert store.files.exists(key)
    assert store.files.get(key) == b"first"


# --- documents ----------------------------------------------------------------


def test_upload_events_are_one_row_each(store):
    doc_id = uid()

    first = store.documents.record_upload(doc_id, "a.pdf", "pdf", 10)
    second = store.documents.record_upload(doc_id, "a.pdf", "pdf", 10)

    assert first != second


def test_document_save_and_read_back(store):
    doc = make_document()
    texts = [f"Klausel {uid()}", f"Klausel {uid()}", f"Klausel {uid()}"]
    clauses = [make_clause(i, t) for i, t in enumerate(texts)]

    store.documents.save(doc, clauses, {c.content_hash: c.text for c in clauses})

    stored = store.documents.get(doc.document_id)
    assert stored.filename == "agb.pdf" and stored.created_at is not None
    got = store.documents.clauses(doc.document_id)
    assert [c.position for c in got] == [0, 1, 2]
    assert [c.text for c in got] == texts
    assert got[0].rects == clauses[0].rects and got[0].heading == "§ 6 Haftung"
    assert got[2].in_scope is False


def test_find_parsed_is_the_parse_cache(store):
    doc = make_document()
    store.documents.save(doc, [make_clause(0, f"x {uid()}")], {})

    assert store.documents.find_parsed(doc.document_id, "p1", "s1") is not None
    assert store.documents.find_parsed(doc.document_id, "p2", "s1") is None
    assert store.documents.find_parsed(doc.document_id, "p1", "s2") is None
    assert store.documents.find_parsed(uid(), "p1", "s1") is None


def test_saving_the_same_document_twice_reuses_it(store):
    doc = make_document()
    clauses = [make_clause(0, f"a {uid()}")]
    store.documents.save(doc, clauses, {})
    first = store.documents.get(doc.document_id)

    store.documents.save(doc, clauses, {})

    assert store.documents.get(doc.document_id) == first
    assert len(store.documents.clauses(doc.document_id)) == 1


def test_a_new_parser_version_replaces_the_parse(store):
    doc_id = uid()
    store.documents.save(make_document(doc_id), [make_clause(0, f"a {uid()}")], {})
    newer = make_document(doc_id, parser="p2")
    new_clauses = [make_clause(0, f"b {uid()}"), make_clause(1, f"c {uid()}")]

    store.documents.save(newer, new_clauses, {})

    assert store.documents.find_parsed(doc_id, "p1", "s1") is None
    assert store.documents.find_parsed(doc_id, "p2", "s1") is not None
    assert len(store.documents.clauses(doc_id)) == 2


def test_clause_text_is_stored_once_across_documents(store):
    shared = f"Wir haften nicht. {uid()}"
    a, b = make_document(), make_document()

    store.documents.save(a, [make_clause(0, shared)], {})
    store.documents.save(b, [make_clause(0, shared), make_clause(1, f"anders {uid()}")], {})

    assert store.documents.clauses(a.document_id)[0].text == shared
    assert store.documents.clauses(b.document_id)[0].content_hash == content_hash(shared)
    if hasattr(store.documents, "text_count"):
        assert store.documents.text_count() == 2


def test_list_returns_newest_first_up_to_the_limit(store):
    docs = [make_document() for _ in range(3)]
    for d in docs:
        store.documents.save(d, [], {})

    listed = store.documents.list(limit=3)

    assert [d.document_id for d in listed] == [d.document_id for d in reversed(docs)]
    assert len(store.documents.list(limit=1)) == 1


# --- verdicts and caches --------------------------------------------------------


def test_verdict_insert_or_reuse(store):
    h = content_hash(uid())
    first = store.verdicts.save(make_verdict(h, reason="Erste."))

    again = store.verdicts.save(make_verdict(h, verdict="potentially_void", reason="Zweite."))

    assert again == first and again.reason_de == "Erste." and again.verdict == "valid"
    assert store.verdicts.get(h, "v1") == first
    assert first.observations.judge == "obs-1" and first.check == "threshold"
    assert first.basis_scores == {"bgb-309-7-a": 0.9}


def test_a_new_agent_version_is_a_new_row(store):
    h = content_hash(uid())
    store.verdicts.save(make_verdict(h, "v1"))
    store.verdicts.save(make_verdict(h, "v2", verdict="uncertain"))

    assert store.verdicts.get(h, "v1").verdict == "valid"
    assert store.verdicts.get(h, "v2").verdict == "uncertain"
    assert store.verdicts.get(h, "v3") is None


def test_embedding_insert_or_reuse(store):
    h, model = content_hash(uid()), "jina-v3"

    assert store.caches.get_embedding(h, model) is None
    store.caches.put_embedding(h, model, [0.1, 0.2])
    store.caches.put_embedding(h, model, [9.0, 9.0])

    assert store.caches.get_embedding(h, model) == [0.1, 0.2]
    assert store.caches.get_embedding(h, "other-model") is None


def test_retrieval_insert_or_reuse_and_key(store):
    h = content_hash(uid())
    first = [{"chunk_id": "bgb-307", "score": 0.9}]

    assert store.caches.get_retrieval(h, "i1", "p1") is None
    store.caches.put_retrieval(h, "i1", "p1", first)
    store.caches.put_retrieval(h, "i1", "p1", [{"chunk_id": "other"}])

    assert store.caches.get_retrieval(h, "i1", "p1") == first
    assert store.caches.get_retrieval(h, "i2", "p1") is None
    assert store.caches.get_retrieval(h, "i1", "p2") is None


# --- reviews ------------------------------------------------------------------


def test_review_create_is_idempotent_and_round_trips(store):
    review = make_review()
    created = store.reviews.create(review)

    assert created.created_at is not None and created.scope == ["liability", "warranty"]
    assert store.reviews.create(review).created_at == created.created_at
    assert store.reviews.get(review.review_id) == created
    assert store.reviews.get(uid()) is None


def test_active_for_dedupes_queued_and_running_only(store):
    doc = uid()
    assert store.reviews.active_for(doc, "v1") is None

    queued = store.reviews.create(make_review(doc))
    assert store.reviews.active_for(doc, "v1").review_id == queued.review_id
    assert store.reviews.active_for(doc, "v2") is None  # another version is another review

    store.reviews.set_status(queued.review_id, "running")
    assert store.reviews.active_for(doc, "v1").review_id == queued.review_id

    store.reviews.set_status(queued.review_id, "finished")
    assert store.reviews.active_for(doc, "v1") is None


def test_latest_for_returns_the_newest_review(store):
    doc = uid()
    assert store.reviews.latest_for(doc) is None
    store.reviews.create(make_review(doc))
    second = store.reviews.create(make_review(doc, "v2"))

    assert store.reviews.latest_for(doc).review_id == second.review_id


def test_status_transitions_stamp_timings_and_error(store):
    review = store.reviews.create(make_review())

    store.reviews.set_status(review.review_id, "running")
    running = store.reviews.get(review.review_id)
    assert running.started_at is not None and running.finished_at is None
    assert review.review_id in [r.review_id for r in store.reviews.running()]

    store.reviews.set_status(review.review_id, "failed", "boom")
    failed = store.reviews.get(review.review_id)
    assert failed.status == "failed" and failed.error == "boom"
    assert failed.finished_at is not None and failed.started_at == running.started_at
    assert review.review_id not in [r.review_id for r in store.reviews.running()]


def test_counts_cost_and_trace(store):
    review = store.reviews.create(make_review())

    store.reviews.update_counts(review.review_id, ReviewCounts(total=5, done=3, valid=2), 0.12)
    store.reviews.set_trace(review.review_id, "trace-1")

    got = store.reviews.get(review.review_id)
    assert got.counts.total == 5 and got.counts.done == 3 and got.cost_usd == pytest.approx(0.12)
    assert got.trace_id == "trace-1"


def test_clause_statuses_upsert_per_review(store):
    review = store.reviews.create(make_review())
    other = store.reviews.create(make_review())

    store.reviews.set_clause_status(review.review_id, 1, "queued")
    store.reviews.set_clause_status(review.review_id, 0, "checking")
    store.reviews.set_clause_status(review.review_id, 1, "failed", "timeout")
    store.reviews.set_clause_status(other.review_id, 1, "done")

    assert store.reviews.clause_statuses(review.review_id) == {
        0: ("checking", None),
        1: ("failed", "timeout"),
    }
    assert store.reviews.clause_statuses(uid()) == {}


# --- events -------------------------------------------------------------------


def test_event_seq_starts_at_one_per_review_and_replays(store):
    a, b = uid(), uid()
    first = store.events.append(a, "RUN_STARTED", {"n": 1})
    store.events.append(a, "CUSTOM", {"n": 2})
    store.events.append(a, "RUN_FINISHED", {"n": 3})
    other = store.events.append(b, "RUN_STARTED", {})

    assert first.seq == 1 and other.seq == 1
    assert [e.seq for e in store.events.since(a)] == [1, 2, 3]
    assert [e.seq for e in store.events.since(a, after_seq=1)] == [2, 3]
    assert [e.payload for e in store.events.since(a, 2)] == [{"n": 3}]
    assert list(store.events.since(a, 3)) == []


def test_concurrent_appends_get_gap_free_unique_seqs(store):
    review_id, workers, per_worker = uid(), 8, 6
    seen: list[int] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(per_worker):
                event = store.events.append(review_id, "CUSTOM", {})
                with lock:
                    seen.append(event.seq)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    total = workers * per_worker
    assert sorted(seen) == list(range(1, total + 1))
    assert [e.seq for e in store.events.since(review_id)] == list(range(1, total + 1))


# --- decisions and feedback ------------------------------------------------------


def test_decision_upsert_on_review_and_position(store):
    rid = uid()
    store.decisions.put(Decision(review_id=rid, position=3, action="accept"))

    updated = store.decisions.put(
        Decision(
            review_id=rid,
            position=3,
            action="override",
            corrected_verdict="valid",
            corrected_basis=["bgb-307"],
            note="Anwalt: zulässig",
            decided_by="anna",
        )
    )
    store.decisions.put(Decision(review_id=rid, position=1, action="dismiss"))

    got = store.decisions.for_review(rid)
    assert list(got) == [1, 3]
    assert got[3].action == "override" and got[3].corrected_basis == ["bgb-307"]
    assert got[3].decided_at is not None and updated.note == "Anwalt: zulässig"
    assert store.decisions.for_review(uid()) == {}


def _feedback(rid, position, vote="down", fid=None, **kw) -> Feedback:
    return Feedback(
        feedback_id=fid or uid(),
        review_id=rid,
        position=position,
        agent_version="v1",
        vote=vote,
        **kw,
    )


def test_feedback_upserts_on_feedback_id(store):
    rid, fid = uid(), uid()
    store.feedback.put(_feedback(rid, 0, "up", fid))

    changed = store.feedback.put(
        _feedback(rid, 0, "down", fid, reasons=["verdict_wrong"], comment="falsch")
    )

    got = store.feedback.for_review(rid)
    assert list(got) == [0]
    assert got[0].vote == "down" and got[0].reasons == ["verdict_wrong"]
    assert changed.comment == "falsch" and got[0].created_at is not None


def test_feedback_for_review_is_the_latest_vote_per_position(store):
    rid = uid()
    store.feedback.put(_feedback(rid, 0, "up"))
    newest = store.feedback.put(_feedback(rid, 0, "down", reasons=["basis_wrong"]))
    store.feedback.put(_feedback(rid, 1, "up"))
    store.feedback.put(_feedback(uid(), 0, "down"))  # another review

    got = store.feedback.for_review(rid)

    assert sorted(got) == [0, 1]
    assert got[0].feedback_id == newest.feedback_id

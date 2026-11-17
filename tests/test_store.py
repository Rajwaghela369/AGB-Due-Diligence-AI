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


def test_verdict_get_many_is_set_based_and_versioned(store):
    first, second, missing = (content_hash(uid()) for _ in range(3))
    store.verdicts.save(make_verdict(first, "v1"))
    store.verdicts.save(make_verdict(second, "v1", verdict="uncertain"))
    store.verdicts.save(make_verdict(first, "v2", verdict="potentially_void"))

    found = store.verdicts.get_many([second, missing, first, first], "v1")

    assert set(found) == {first, second}
    assert found[first].verdict == "valid"
    assert found[second].verdict == "uncertain"
    assert store.verdicts.get_many([], "v1") == {}


def test_embedding_insert_or_reuse(store):
    h, model = content_hash(uid()), "jina-v3"

    assert store.caches.get_embedding(h, model) is None
    store.caches.put_embedding(h, model, [0.1, 0.2])
    store.caches.put_embedding(h, model, [9.0, 9.0])

    assert store.caches.get_embedding(h, model) == [0.1, 0.2]
    assert store.caches.get_embedding(h, "other-model") is None


def test_topic_cache_insert_or_reuse_per_model(store):
    a, b = uid(), uid()
    store.caches.put_topics("jina-v3", {a: "liability", b: "other"})
    store.caches.put_topics("jina-v3", {a: "warranty"})  # first writer wins
    store.caches.put_topics("jina-v3", {})

    assert store.caches.get_topics([a, b, uid()], "jina-v3") == {a: "liability", b: "other"}
    assert store.caches.get_topics([a], "jina-v4") == {}
    assert store.caches.get_topics([], "jina-v3") == {}


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


def test_one_active_review_per_document_and_version(store):
    doc = uid()
    first = store.reviews.create(make_review(doc))
    # A second active review of the same document and version loses to the first.
    assert store.reviews.create(make_review(doc)).review_id == first.review_id
    assert store.reviews.create(make_review(doc, "v2")).review_id != first.review_id

    store.reviews.set_status(first.review_id, "finished")
    second = store.reviews.create(make_review(doc))
    assert second.review_id != first.review_id
    with pytest.raises(Exception):  # noqa: B017 - ValueError in memory, UniqueViolation in Postgres
        store.reviews.set_status(first.review_id, "running")


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


def test_set_clause_statuses_upserts_many_in_one_call(store):
    review = store.reviews.create(make_review())
    store.reviews.set_clause_status(review.review_id, 0, "queued")

    store.reviews.set_clause_statuses(
        review.review_id, {0: ("done", None), 1: ("failed", "timeout"), 2: ("queued", None)}
    )
    store.reviews.set_clause_statuses(review.review_id, {})

    assert store.reviews.clause_statuses(review.review_id) == {
        0: ("done", None),
        1: ("failed", "timeout"),
        2: ("queued", None),
    }


def test_summary_text_cache_insert_or_reuse_per_model(store):
    key = uid()
    assert store.caches.get_summary_text(key, "flash") is None
    store.caches.put_summary_text(key, "flash", "Erste Empfehlung.")
    store.caches.put_summary_text(key, "flash", "Zweite Empfehlung.")  # first writer wins

    assert store.caches.get_summary_text(key, "flash") == "Erste Empfehlung."
    assert store.caches.get_summary_text(key, "pro") is None
    assert store.caches.get_summary_text(uid(), "flash") is None


def test_clause_topics_live_on_the_review_rows(store):
    review = store.reviews.create(make_review())
    rid = review.review_id
    store.reviews.set_clause_status(rid, 0, "done")
    store.reviews.set_clause_topics(rid, {0: "liability", 1: "other"})
    store.reviews.set_clause_topics(rid, {})

    assert store.reviews.clause_topics(rid) == {0: "liability", 1: "other"}
    # The topic never changes a status; a row it creates starts queued.
    assert store.reviews.clause_statuses(rid) == {0: ("done", None), 1: ("queued", None)}
    store.reviews.set_clause_status(rid, 1, "out_of_scope")
    assert store.reviews.clause_topics(rid) == {0: "liability", 1: "other"}
    assert store.reviews.clause_topics(store.reviews.create(make_review()).review_id) == {}


def test_awaiting_decisions_is_idle_and_stamps_finished_at(store):
    review = store.reviews.create(make_review())
    store.reviews.set_status(review.review_id, "running")
    store.reviews.set_status(review.review_id, "awaiting_decisions")

    got = store.reviews.get(review.review_id)
    assert got.status == "awaiting_decisions" and got.finished_at is not None
    assert store.reviews.active_for(review.document_id, review.agent_version) is None
    assert review.review_id not in {r.review_id for r in store.reviews.running()}


def test_finished_at_is_the_end_of_the_last_run(store):
    """A decision moving the review between idle statuses keeps `finished_at` (Phase 8
    wall time); a retry's run ending restamps it."""
    review = store.reviews.create(make_review())
    store.reviews.set_status(review.review_id, "running")
    store.reviews.set_status(review.review_id, "awaiting_decisions")
    ended = store.reviews.get(review.review_id).finished_at

    store.reviews.set_status(review.review_id, "finished")
    assert store.reviews.get(review.review_id).finished_at == ended

    store.reviews.set_status(review.review_id, "running")
    store.reviews.set_status(review.review_id, "finished")
    assert store.reviews.get(review.review_id).finished_at > ended


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


def test_append_many_assigns_consecutive_seqs_in_order(store):
    review_id = uid()
    store.events.append(review_id, "RUN_STARTED", {"n": 0})

    stored = store.events.append_many(
        review_id, [("STATE_DELTA", {"n": 1}), ("CUSTOM", {"n": 2}), ("CUSTOM", {"n": 3})]
    )

    assert store.events.append_many(review_id, []) == []
    assert [(e.seq, e.type, e.payload["n"]) for e in stored] == [
        (2, "STATE_DELTA", 1),
        (3, "CUSTOM", 2),
        (4, "CUSTOM", 3),
    ]
    assert [e.seq for e in store.events.since(review_id)] == [1, 2, 3, 4]
    assert store.events.append(review_id, "CUSTOM", {}).seq == 5


def test_concurrent_append_and_append_many_stay_gap_free_and_contiguous(store):
    review_id, workers, rounds, batch = uid(), 6, 4, 5
    errors: list[BaseException] = []

    def work(index: int):
        try:
            for r in range(rounds):
                if index % 2:
                    store.events.append(review_id, "CUSTOM", {"w": index, "r": r})
                else:
                    store.events.append_many(
                        review_id, [("CUSTOM", {"w": index, "r": r, "i": i}) for i in range(batch)]
                    )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    events = list(store.events.since(review_id))
    singles, batches = workers // 2, workers - workers // 2
    total = singles * rounds + batches * rounds * batch
    assert [e.seq for e in events] == list(range(1, total + 1))
    # A batch is never split by another writer: its items are adjacent and in order.
    runs = [(e.payload["w"], e.payload["r"], e.payload.get("i")) for e in events]
    for pos, (w, r, i) in enumerate(runs):
        if i is not None and i < batch - 1:
            assert runs[pos + 1] == (w, r, i + 1)


# --- decisions and feedback ------------------------------------------------------


def _decision(rid, position, action="accept", model="potentially_void", **kw) -> Decision:
    return Decision(
        review_id=rid,
        position=position,
        action=action,
        model_verdict=model,
        model_basis=kw.pop("model_basis", ["bgb-309-7-a"]),
        **kw,
    )


def test_decision_upsert_on_review_and_position(store):
    rid = uid()
    store.decisions.put(_decision(rid, 3), agent_version="v1")

    updated = store.decisions.put(
        _decision(
            rid,
            3,
            "override",
            corrected_verdict="valid",
            corrected_basis=["bgb-307"],
            note="Anwalt: zulässig",
            decided_by="anna",
            reason_de="Zulässig nach § 307.",
        ),
        agent_version="v1",
    )
    store.decisions.put(_decision(rid, 1, "dismiss"), agent_version="v1")

    got = store.decisions.for_review(rid)
    assert list(got) == [1, 3]
    assert got[3].action == "override" and got[3].corrected_basis == ["bgb-307"]
    assert got[3].decided_at is not None and updated.note == "Anwalt: zulässig"
    assert got[3].model_verdict == "potentially_void" and got[3].model_basis == ["bgb-309-7-a"]
    assert got[3].reason_de == "Zulässig nach § 307." and not got[3].reason_unavailable
    assert store.decisions.for_review(uid()) == {}


def test_a_decision_needs_the_model_verdict(store):
    with pytest.raises(ValueError):
        store.decisions.put(
            Decision(review_id=uid(), position=0, action="accept"), agent_version="v1"
        )


def test_three_decisions_leave_three_history_rows_and_one_current(store):
    rid = uid()
    first = store.decisions.put(
        _decision(rid, 2, decided_by="Anna Müller"),
        agent_version="v1",
        request_id="req-1",
    )
    store.decisions.put(
        _decision(rid, 2, "override", corrected_verdict="valid", note="doch wirksam"),
        agent_version="v1",
        request_id="req-2",
    )
    store.decisions.put(_decision(rid, 2, "dismiss"), agent_version="v1", authenticated=True)
    store.decisions.put(_decision(rid, 5), agent_version="v1")

    assert list(store.decisions.for_review(rid)) == [2, 5]
    history = store.decisions.history(rid, 2)
    assert [e.action for e in history] == ["accept", "override", "dismiss"]
    assert [e.previous_action for e in history] == [None, "accept", "override"]
    assert history[0].event_id < history[1].event_id < history[2].event_id
    # from: what was in force before; to: what is in force after.
    assert (history[0].from_verdict, history[0].to_verdict) == (
        "potentially_void",
        "potentially_void",
    )
    assert history[0].from_basis == history[0].to_basis == ["bgb-309-7-a"]
    # An override without a corrected basis keeps the model's §.
    assert (history[1].from_verdict, history[1].to_verdict) == ("potentially_void", "valid")
    assert history[1].to_basis == ["bgb-309-7-a"]
    assert (history[2].from_verdict, history[2].to_verdict, history[2].to_basis) == (
        "valid",
        None,
        [],
    )
    assert history[0].decided_by == "Anna Müller" and history[0].at == first.decided_at
    assert [e.request_id for e in history] == ["req-1", "req-2", None]
    assert [e.authenticated for e in history] == [False, False, True]
    assert history[1].note == "doch wirksam" and history[0].agent_version == "v1"
    assert [e.position for e in store.decisions.history(rid)] == [2, 2, 2, 5]
    assert store.decisions.history(uid()) == []


def test_concurrent_decisions_chain_their_history(store):
    rid = uid()
    verdicts = ["valid", "uncertain"] * 5

    def decide(verdict):
        store.decisions.put(
            _decision(rid, 0, "override", corrected_verdict=verdict), agent_version="v1"
        )

    threads = [threading.Thread(target=decide, args=(v,)) for v in verdicts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = store.decisions.history(rid, 0)
    assert len(history) == len(verdicts)
    # Each event starts where the one before it ended: no lost update in the chain.
    for before, after in zip(history, history[1:], strict=False):
        assert after.from_verdict == before.to_verdict
    current = store.decisions.for_review(rid)[0]
    assert current.corrected_verdict == history[-1].to_verdict


def test_agreement_counts_current_decisions_per_version_and_verdict(store):
    version = f"v-{uid()}"
    review = make_review(version=version, status="finished")
    store.reviews.create(review)
    rid = review.review_id
    put = store.decisions.put
    put(_decision(rid, 0, "accept", model="potentially_void"), agent_version=version)
    put(  # § changed only: still agreement
        _decision(
            rid,
            1,
            "override",
            model="potentially_void",
            corrected_verdict="potentially_void",
            corrected_basis=["bgb-307"],
        ),
        agent_version=version,
    )
    put(
        _decision(rid, 2, "override", model="potentially_void", corrected_verdict="valid"),
        agent_version=version,
    )
    put(_decision(rid, 3, "dismiss", model="uncertain"), agent_version=version)
    put(_decision(rid, 4, "accept", model="valid"), agent_version=version)
    # Re-deciding replaces: position 4 was accepted, now overridden.
    put(
        _decision(rid, 4, "override", model="valid", corrected_verdict="potentially_void"),
        agent_version=version,
    )

    [agreement] = store.metrics.agreement(version)
    assert agreement.agent_version == version
    assert (agreement.decided, agreement.kept, agreement.overridden, agreement.dismissed) == (
        5,
        2,
        2,
        1,
    )
    assert agreement.rate == pytest.approx(0.5)
    void = agreement.by_verdict.potentially_void
    assert (void.decided, void.kept, void.overridden, void.rate) == (3, 2, 1, pytest.approx(2 / 3))
    uncertain = agreement.by_verdict.uncertain
    assert (uncertain.decided, uncertain.dismissed, uncertain.rate) == (1, 1, None)
    assert agreement.by_verdict.valid.overridden == 1
    assert agreement.last_decided_at is not None
    assert store.metrics.agreement(f"v-{uid()}") == []
    assert version in {a.agent_version for a in store.metrics.agreement()}


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


def test_feedback_get_by_id(store):
    rid, fid = uid(), uid()
    assert store.feedback.get(fid) is None
    store.feedback.put(_feedback(rid, 3, "up", fid))
    assert store.feedback.get(fid).vote == "up"


def test_feedback_delete_is_idempotent(store):
    rid, fid = uid(), uid()
    store.feedback.put(_feedback(rid, 3, "up", fid))

    store.feedback.delete(fid)
    store.feedback.delete(fid)

    assert store.feedback.get(fid) is None
    assert store.feedback.for_review(rid) == {}


def test_feedback_for_review_is_the_latest_vote_per_position(store):
    rid = uid()
    store.feedback.put(_feedback(rid, 0, "up"))
    newest = store.feedback.put(_feedback(rid, 0, "down", reasons=["basis_wrong"]))
    store.feedback.put(_feedback(rid, 1, "up"))
    store.feedback.put(_feedback(uid(), 0, "down"))  # another review

    got = store.feedback.for_review(rid)

    assert sorted(got) == [0, 1]
    assert got[0].feedback_id == newest.feedback_id


def test_feedback_upsert_keeps_review_and_position_and_stores_observations(store):
    rid, fid = uid(), uid()
    store.feedback.put(_feedback(rid, 2, "up", fid, observations={"clause_span": "o1"}))

    changed = store.feedback.put(
        _feedback(uid(), 9, "down", fid, observations={"judge": "o2", "retrieval": None})
    )

    assert (changed.review_id, changed.position) == (rid, 2)
    assert changed.observations == {"judge": "o2", "retrieval": None}
    assert store.feedback.for_review(rid)[2].observations == changed.observations


@pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL not set")
@pytest.mark.parametrize(
    "statement",
    [
        "update decision_events set note = 'changed' where review_id = %s",
        "delete from decision_events where review_id = %s",
        "truncate decision_events",
    ],
)
def test_postgres_refuses_to_change_decision_history(tmp_path, _migrated, statement):
    import psycopg

    from app.store.files import LocalFileStore
    from app.store.postgres import postgres_store

    store = postgres_store(LocalFileStore(tmp_path), _connect)
    rid = uid()
    store.decisions.put(_decision(rid, 0), agent_version="v1")

    with pytest.raises(psycopg.errors.InsufficientPrivilege), _connect() as conn:
        conn.execute(statement, (rid,) if "%s" in statement else ())
    assert len(store.decisions.history(rid)) == 1

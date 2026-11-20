"""Topic tagging at review start makes the scope filter real (Phase 7 Wave 0 carry-over).

The router is injected, so nothing here calls Jina.
"""

import pytest
from core.hashing import content_hash

from app.domain import ClauseSpan
from app.review.runner import count, load_entries, review_clauses
from app.review.topics import tag_topics
from app.store.memory import memory_store
from tests.test_review_runner import FakeJudge, _collect, _document, _finish, _service

TOPICS = {
    "Die Haftung für leichte Fahrlässigkeit ist ausgeschlossen.": "liability",
    "Mängelansprüche verjähren in einem Jahr.": "warranty",
    "Gerichtsstand ist Berlin.": "other",
}
TEXTS = list(TOPICS)


class FakeRouter:
    """Topics by text; records every batch it was asked for."""

    def __init__(self, topics=TOPICS, fail=False):
        self.topics = topics
        self.fail = fail
        self.batches: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[str]:
        self.batches.append(list(texts))
        if self.fail:
            raise ConnectionError("Jina is down")
        return [self.topics[text] for text in texts]


def test_scope_liability_judges_only_the_liability_clause():
    store = memory_store()
    judge = FakeJudge()
    router = FakeRouter()
    service = _service(store, judge, router=router, router_model="jina-test")
    review = service.start(_document(store, TEXTS), ["liability"])
    finished = _finish(service, review.review_id)

    assert judge.calls == [TEXTS[0]]
    assert router.batches == [TEXTS]  # one batched call for the whole document
    findings = service.findings(review.review_id)
    assert [(f.status, f.clause.topic) for f in findings] == [
        ("done", "liability"),
        ("out_of_scope", "warranty"),
        ("out_of_scope", "other"),
    ]
    assert (finished.counts.total, finished.counts.out_of_scope) == (1, 2)
    # The topics are on the review's own rows, so a reload scopes the same way.
    assert store.reviews.clause_topics(review.review_id) == {
        0: "liability",
        1: "warranty",
        2: "other",
    }


def test_snapshot_and_counts_carry_out_of_scope_from_topics():
    store = memory_store()
    service = _service(store, FakeJudge(), router=FakeRouter())
    review = service.start(_document(store, TEXTS), ["liability", "warranty"])
    events = _collect(service, review.review_id)

    snapshot = next(e for e in events if e.type == "STATE_SNAPSHOT").payload["snapshot"]
    assert {pos: c["status"] for pos, c in snapshot["clauses"].items()}["2"] == "out_of_scope"
    assert events[-1].payload["result"]["counts"]["out_of_scope"] == 1


def test_topics_are_cached_by_text_and_model():
    store = memory_store()
    router = FakeRouter()
    first = _service(store, FakeJudge(), router=router, router_model="jina-test")
    _finish(first, first.start(_document(store, TEXTS), ["liability"]).review_id)
    # Another document sharing two clauses: only the new text reaches the router.
    other = [TEXTS[0], TEXTS[2], "Der Kunde haftet für Schäden."]
    router.topics = {**TOPICS, other[2]: "liability"}
    second = _service(store, FakeJudge(), router=router, router_model="jina-test")
    review = second.start(_document(store, other), ["liability"])
    _finish(second, review.review_id)

    assert router.batches == [TEXTS, [other[2]]]
    cached = store.caches.get_topics([content_hash(t) for t in other], "jina-test")
    assert sorted(cached.values()) == ["liability", "liability", "other"]


def test_a_router_failure_judges_every_clause():
    store = memory_store()
    judge = FakeJudge()
    service = _service(store, judge, router=FakeRouter(fail=True))
    review = service.start(_document(store, TEXTS), ["liability"])
    finished = _finish(service, review.review_id)

    assert sorted(judge.calls) == sorted(TEXTS)
    assert finished.status == "finished" and finished.counts.out_of_scope == 0


def test_without_a_router_nothing_is_tagged():
    store = memory_store()
    judge = FakeJudge()
    service = _service(store, judge)
    review = service.start(_document(store, TEXTS), ["liability"])
    _finish(service, review.review_id)

    assert len(judge.calls) == 3
    assert store.reviews.clause_topics(review.review_id) == {}


def test_ingest_out_of_scope_clauses_are_never_sent_to_the_router():
    store = memory_store()
    router = FakeRouter()
    clauses = [
        ClauseSpan(position=i, text=t, content_hash=f"h{i}", in_scope=i != 1)
        for i, t in enumerate(TEXTS)
    ]
    tags = tag_topics(clauses, router=router, model="m", caches=store.caches)

    assert router.batches == [[TEXTS[0], TEXTS[2]]]
    assert tags == {0: "liability", 2: "other"}


def test_a_router_returning_the_wrong_number_of_topics_tags_nothing():
    store = memory_store()
    clauses = [ClauseSpan(position=0, text=TEXTS[0], content_hash="h0")]

    def router(texts):
        return []

    assert tag_topics(clauses, router=router, model="m", caches=store.caches) == {}
    assert store.caches.get_topics(["h0"], "m") == {}


def test_review_clauses_and_entries_use_the_review_topics():
    store = memory_store()
    service = _service(store, FakeJudge(), router=FakeRouter())
    review = service.start(_document(store, TEXTS), ["warranty"])
    review = _finish(service, review.review_id)

    clauses = review_clauses(store, review)
    entries = load_entries(store, review, clauses)
    assert [entries[p][0] for p in range(3)] == ["out_of_scope", "done", "out_of_scope"]
    assert count(entries, 0).total == 1


@pytest.mark.parametrize("scope", [["liability"], ["warranty"], ["liability", "warranty"]])
def test_every_scope_judges_exactly_its_topics(scope):
    store = memory_store()
    judge = FakeJudge()
    service = _service(store, judge, router=FakeRouter())
    _finish(service, service.start(_document(store, TEXTS), scope).review_id)

    assert sorted(judge.calls) == sorted(t for t in TEXTS if TOPICS[t] in scope)

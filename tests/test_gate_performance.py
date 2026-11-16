"""Regression tests for the Neon round trips found by the Phase 5 live gate."""

from contextlib import contextmanager

from core.hashing import content_hash

from app.domain import ClauseVerdict, Review, ReviewCounts
from app.review.service import build_service
from app.store.files import LocalFileStore
from app.store.memory import memory_store
from app.store.postgres import PostgresVerdictRepo, _ConnectionPool, postgres_store
from tests.test_review_runner import FakeJudge, StubLimiter, _document, _finish, _verdict


def _verdict_row(verdict: ClauseVerdict) -> dict:
    row = verdict.model_dump(mode="python")
    row["check_rule"] = row.pop("check")
    return row


class QueryConnection:
    """A dict-row connection whose result sets are selected from the SQL text."""

    def __init__(self, review: Review, clauses: list[dict], verdicts: list[dict]) -> None:
        self.review = review
        self.clauses = clauses
        self.verdicts = verdicts
        self.queries: list[str] = []
        self.description = object()
        self._rows: list[dict] = []

    def cursor(self, **_):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append(sql)
        if "from reviews where review_id" in sql:
            self._rows = [
                {
                    **self.review.model_dump(mode="python"),
                    "counts": self.review.counts.model_dump(mode="python"),
                }
            ]
        elif "from document_clauses" in sql:
            self._rows = self.clauses
        elif "from review_clauses" in sql:
            self._rows = [
                {"position": clause["position"], "status": "done", "error": None}
                for clause in self.clauses
            ]
        elif "from verdicts" in sql:
            hashes = set(params[0]) if "any(%s)" in sql else {params[0]}
            self._rows = [row for row in self.verdicts if row["content_hash"] in hashes]
        elif "from decisions" in sql or "from feedback" in sql:
            self._rows = []
        else:
            raise AssertionError(f"unexpected query: {sql}")
        return self

    def fetchall(self):
        return list(self._rows)


def _fake_connect(conn):
    @contextmanager
    def connect():
        yield conn

    return connect


def _clause_rows(texts: list[str]) -> list[dict]:
    return [
        {
            "position": position,
            "number": str(position + 1),
            "heading": None,
            "text": text,
            "content_hash": content_hash(text),
            "page": None,
            "rects": [],
            "start": None,
            "end": None,
            "in_scope": True,
            "topic": None,
        }
        for position, text in enumerate(texts)
    ]


def test_findings_uses_a_constant_number_of_selects(tmp_path):
    texts = [f"clause {position}" for position in range(40)]
    clauses = _clause_rows(texts)
    review = Review(
        review_id="review-1",
        document_id="document-1",
        agent_version="agent-v1",
        scope=["liability", "warranty"],
        status="finished",
        counts=ReviewCounts(total=len(texts), done=len(texts), valid=len(texts)),
    )
    verdicts = [_verdict_row(_verdict(text)) for text in texts]
    connection = QueryConnection(review, clauses, verdicts)
    store = postgres_store(LocalFileStore(tmp_path), _fake_connect(connection))
    service = build_service(store, FakeJudge(), StubLimiter())

    findings = service.findings(review.review_id)

    assert len(findings) == len(texts)
    assert all(finding.verdict is not None for finding in findings)
    assert len(connection.queries) == 6
    assert sum("from verdicts" in sql for sql in connection.queries) == 1


def test_fully_cached_run_reads_all_verdicts_in_one_query():
    texts = [f"cached {position}" for position in range(40)]
    store = memory_store()
    document_id = _document(store, texts)
    review = Review(
        review_id="unused",
        document_id=document_id,
        agent_version="agent-v1",
        scope=["liability"],
        status="queued",
    )
    rows = [_verdict_row(_verdict(text)) for text in texts]
    connection = QueryConnection(review, [], rows)
    store.verdicts = PostgresVerdictRepo(_fake_connect(connection))
    judge = FakeJudge()
    service = build_service(store, judge, StubLimiter(), max_concurrency=4, poll_seconds=0.01)

    started = service.start(document_id, ["liability"])
    finished = _finish(service, started.review_id)

    verdict_queries = [sql for sql in connection.queries if "from verdicts" in sql]
    assert judge.calls == []
    assert finished.counts.cache_hits == len(texts)
    assert len(verdict_queries) == 1
    assert "any(%s)" in verdict_queries[0]


def test_connection_pool_reuses_a_checked_in_connection(monkeypatch):
    class Connection:
        closed = False

        def __init__(self):
            self.commits = 0
            self.rollbacks = 0

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    created = []
    pool = _ConnectionPool(max_size=2)

    def new():
        created.append(Connection())
        return created[-1]

    monkeypatch.setattr(pool, "_new", new)
    with pool.connection() as first:
        pass
    with pool.connection() as second:
        pass

    assert first is second
    assert len(created) == 1
    assert first.commits == 2
    pool.close()
    assert first.closed


class CountingStore:
    """Wraps a store and counts every call that writes, to show the cost per clause."""

    WRITES = {
        ("reviews", "set_clause_status"),
        ("reviews", "set_clause_statuses"),
        ("reviews", "update_counts"),
        ("events", "append"),
        ("events", "append_many"),
    }

    def __init__(self, inner) -> None:
        self._inner = inner
        self.writes: list[tuple[str, str]] = []

    def __getattr__(self, name):
        repo = getattr(self._inner, name)
        if name not in ("reviews", "events"):
            return repo
        outer = self

        class Counted:
            def __getattr__(self, method):
                target = getattr(repo, method)
                if (name, method) not in outer.WRITES:
                    return target

                def call(*args, **kwargs):
                    outer.writes.append((name, method))
                    return target(*args, **kwargs)

                return call

        return Counted()


def _cached_review_writes(clause_count: int) -> tuple[int, list]:
    inner = memory_store()
    texts = [f"clause {i}" for i in range(clause_count)]
    for text in texts:
        inner.verdicts.save(_verdict(text))
    store = CountingStore(inner)
    judge = FakeJudge()
    service = build_service(store, judge, StubLimiter(), max_concurrency=2, poll_seconds=0.01)
    review = service.start(_document(inner, texts), ["liability"])
    finished = _finish(service, review.review_id)
    assert finished.status == "finished"
    assert finished.counts.cache_hits == clause_count and judge.calls == []
    events = list(inner.events.since(review.review_id))
    return len(store.writes), events


def test_fully_cached_review_makes_constant_store_writes():
    small, _ = _cached_review_writes(3)
    large, events = _cached_review_writes(36)

    assert large == small  # O(1) in the number of clauses
    assert large <= 8
    deltas = [e for e in events if e.type == "STATE_DELTA"]
    assert len(deltas) == 1
    assert [op["path"] for op in deltas[0].payload["delta"]][-1] == "/review/counts"
    assert len(deltas[0].payload["delta"]) == 37
    assert [e.type for e in events].count("CUSTOM") == 1
    assert [e.seq for e in events] == list(range(1, len(events) + 1))

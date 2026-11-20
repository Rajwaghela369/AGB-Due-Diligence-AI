"""Login and per-user access through the real app (9.E), with a fake issuer.

The matrix: user A owns a document with a finished review, decisions and feedback; user B
calls every route that takes one of A's ids and must get 404 `not_found`, never 403 and
never A's data. A coverage test lists every route of the app, so a new route that takes a
document or review id fails here until it is in the matrix.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.testing import FakeChatModel, KeywordJudge
from app.review.feedback import FeedbackService
from app.review.limiter import build_limiter
from app.review.service import build_service
from app.store.memory import memory_store
from tests.fake_issuer import FakeIssuer, auth_config, bearer

TEXT = (
    "§ 1 Geltungsbereich\nDiese Bedingungen gelten für alle Verträge.\n\n"
    "§ 2 Haftung\nDie Haftung für Personenschäden ist ausgeschlossen.\n"
)


class _Recording:
    def __init__(self) -> None:
        self.api = None

    def create_score(self, **_kwargs) -> None:
        pass


@pytest.fixture
def world():
    issuer = FakeIssuer()
    store = memory_store()
    feedback = FeedbackService(store, _Recording())
    service = build_service(
        store,
        KeywordJudge(delay=0),
        build_limiter({}),
        feedback=feedback,
        chat_model=FakeChatModel(delay=0),
    )
    service.admin_role = "agb-admin"
    application = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=feedback,
        settings=auth_config(api={"rate_limit_per_minute": 100000}),
        oidc_verifier=issuer.verifier(),
    )
    with TestClient(application) as client:
        a = bearer(issuer.token("user-a"))
        b = bearer(issuer.token("user-b"))
        admin = bearer(issuer.token("admin", realm_access={"roles": ["agb-admin"]}))
        document = client.post(
            "/api/v1/documents", json={"text": TEXT, "filename": "a.txt"}, headers=a
        ).json()["document"]
        review = client.post(
            "/api/v1/reviews",
            json={"document_id": document["document_id"], "scope": []},
            headers=a,
        ).json()["review"]
        assert service.wait(review["review_id"], timeout=10)
        yield {
            "client": client,
            "store": store,
            "service": service,
            "issuer": issuer,
            "a": a,
            "b": b,
            "admin": admin,
            "document_id": document["document_id"],
            "review_id": review["review_id"],
        }


def _void_position(w) -> int:
    findings = w["client"].get(f"/api/v1/reviews/{w['review_id']}/findings", headers=w["a"])
    return next(
        f["position"]
        for f in findings.json()["findings"]
        if f["verdict"] and f["verdict"]["verdict"] == "potentially_void"
    )


# (method, path template, json body) for every route that takes a document or review id.
MATRIX = [
    ("GET", "/api/v1/documents/{d}", None),
    ("GET", "/api/v1/documents/{d}/file", None),
    ("DELETE", "/api/v1/documents/{d}", None),
    ("DELETE", "/api/v1/documents/{d}?dry_run=true", None),
    ("POST", "/api/v1/reviews", {"document_id": "{d}", "scope": []}),
    ("GET", "/api/v1/reviews/{r}", None),
    ("GET", "/api/v1/reviews/{r}/findings", None),
    ("GET", "/api/v1/reviews/{r}/events", None),
    ("POST", "/api/v1/reviews/{r}/clauses/{p}/retry", None),
    ("POST", "/api/v1/reviews/{r}/clauses/{p}/explain", None),
    ("PUT", "/api/v1/reviews/{r}/clauses/{p}/decision", {"action": "accept"}),
    ("GET", "/api/v1/reviews/{r}/audit", None),
    ("GET", "/api/v1/reviews/{r}/summary", None),
    ("GET", "/api/v1/reviews/{r}/export?format=md&draft=true", None),
    ("POST", "/api/v1/reviews/{r}/clauses/{p}/chat", {"message": "Bitte umformulieren"}),
    ("POST", "/api/v1/feedback", {"review_id": "{r}", "position": "{p}", "vote": "up"}),
]


def _fill(value, w, position):
    if isinstance(value, str):
        return (
            value.replace("{d}", w["document_id"])
            .replace("{r}", w["review_id"])
            .replace("{p}", str(position))
        )
    if isinstance(value, dict):
        filled = {key: _fill(item, w, position) for key, item in value.items()}
        if filled.get("position") is not None:
            filled["position"] = int(filled["position"])
        return filled
    return value


@pytest.mark.parametrize(("method", "path", "body"), MATRIX)
def test_another_user_gets_404_on_every_route(world, method, path, body):
    position = _void_position(world)
    response = world["client"].request(
        method,
        _fill(path, world, position),
        json=_fill(body, world, position),
        headers=world["b"],
    )
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    assert world["review_id"] not in response.text or "/reviews" in path


def test_nothing_of_another_user_changed_or_leaked(world):
    client, store = world["client"], world["store"]
    position = _void_position(world)
    for method, path, body in MATRIX:
        client.request(
            method,
            _fill(path, world, position),
            json=_fill(body, world, position),
            headers=world["b"],
        )
    assert store.documents.get(world["document_id"]) is not None
    assert store.decisions.for_review(world["review_id"]) == {}
    assert store.feedback.for_review(world["review_id"]) == {}
    listing = client.get("/api/v1/documents", headers=world["b"]).json()
    assert listing["items"] == []
    own = client.get("/api/v1/documents", headers=world["a"]).json()
    assert [item["document"]["document_id"] for item in own["items"]] == [world["document_id"]]


def test_the_owner_reaches_every_route(world):
    client = world["client"]
    position = _void_position(world)
    ok = {
        "GET /api/v1/documents/{d}": 200,
        "GET /api/v1/reviews/{r}/events": 200,
        "PUT /api/v1/reviews/{r}/clauses/{p}/decision": 200,
        "GET /api/v1/reviews/{r}/audit": 200,
        "GET /api/v1/reviews/{r}/export?format=md&draft=true": 200,
    }
    for key, status in ok.items():
        method, path = key.split(" ", 1)
        body = next(b for m, p, b in MATRIX if m == method and p == path)
        response = client.request(
            method,
            _fill(path, world, position),
            json=_fill(body, world, position),
            headers=world["a"],
        )
        assert response.status_code == status, (key, response.text)


def test_a_decision_records_the_signed_in_user_not_the_header(world):
    client = world["client"]
    position = _void_position(world)
    response = client.put(
        f"/api/v1/reviews/{world['review_id']}/clauses/{position}/decision",
        json={"action": "accept"},
        headers={**world["a"], "X-Reviewer": "Mallory"},
    )
    assert response.status_code == 200
    assert response.json()["decision"]["decided_by"] == "Anwältin user-a"
    audit = client.get(f"/api/v1/reviews/{world['review_id']}/audit", headers=world["a"]).json()
    assert audit["events"][-1]["authenticated"] is True
    assert audit["events"][-1]["decided_by"] == "Anwältin user-a"


def test_the_same_file_by_two_users_gives_two_documents(world):
    client = world["client"]
    mine = client.post(
        "/api/v1/documents", json={"text": TEXT, "filename": "b.txt"}, headers=world["b"]
    )
    assert mine.status_code == 200
    body = mine.json()
    assert body["document"]["document_id"] != world["document_id"]
    assert body["reused"] is False and body["latest_review"] is None
    assert (
        body["document"]["source_hash"]
        == world["store"].documents.get(world["document_id"]).source_hash
    )


def test_the_admin_role_reads_and_deletes_any_document(world):
    client = world["client"]
    read = client.get(f"/api/v1/documents/{world['document_id']}", headers=world["admin"])
    assert read.status_code == 200
    deleted = client.delete(f"/api/v1/documents/{world['document_id']}", headers=world["admin"])
    assert deleted.status_code == 200, deleted.text
    assert world["store"].documents.get(world["document_id"]) is None
    tombstone = world["store"].deletion.tombstones(world["document_id"])[0]
    assert tombstone.deleted_by == "admin" and tombstone.authenticated is True


def test_without_a_valid_token_every_route_is_401(world):
    client, issuer = world["client"], world["issuer"]
    expired = bearer(issuer.token("user-a", exp=1))
    for headers in ({}, {"Authorization": "Basic abc"}, bearer("garbage"), expired):
        for path in (
            "/api/v1/documents",
            "/api/v1/me",
            "/api/v1/statutes/x",
            "/api/v1/metrics/agreement",
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 401, (path, headers)
            assert response.json()["code"] == "unauthenticated"
    assert client.get("/api/v1/health").status_code == 200


def test_me_reports_the_signed_in_user(world):
    me = world["client"].get("/api/v1/me", headers=world["admin"]).json()
    assert me == {
        "owner_id": "admin",
        "name": "Anwältin admin",
        "roles": ["agb-admin"],
        "authenticated": True,
        "auth_enabled": True,
    }


def test_every_id_route_is_in_the_matrix(world):
    """A new route taking a document or review id must be added to MATRIX (and checked)."""
    covered = {(method, path.split("?")[0]) for method, path, _ in MATRIX}
    for route in world["client"].app.routes:
        path = getattr(route, "path", "")
        if "{document_id}" not in path and "{review_id}" not in path:
            continue
        template = (
            path.replace("{document_id}", "{d}")
            .replace("{review_id}", "{r}")
            .replace("{position}", "{p}")
        )
        for method in route.methods - {"HEAD", "OPTIONS"}:
            assert (method, template) in covered, f"{method} {path} is not in the access matrix"

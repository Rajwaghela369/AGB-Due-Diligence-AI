"""The Phase 9 Wave 0 contracts behave as documented, before Wave 1 builds on them.

Masking: the boundary marks what it masked, a strict provider client refuses anything else
once masking is on, and the Presidio masker is a stub. Owners: documents and reviews carry
one, ids are per owner, and the default owner keeps every pre-Phase 9 id. Deletion and
storage: the protocols exist, the storage bodies raise NotImplementedError, the deletion
bodies (Wave 1) refuse an unknown document, and the route's 200/404/409 mappings work with a
stand-in service. Login: `GET /me` answers the
default owner while login is off and fails closed (501) when it is turned on early.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from core import masking
from core.config import load_yaml
from core.masking import (
    IdentityMasker,
    MaskedText,
    MaskingError,
    PresidioMasker,
    UnmaskedPayload,
    boundary,
    masker_from_config,
)
from core.paths import CONFIGS
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.schemas import DeletionOut
from app.domain import (
    DEFAULT_OWNER_ID,
    DELETION_TABLES,
    DeletionReport,
    Document,
    ErasureTask,
    Principal,
    Review,
    SweepReport,
    Tombstone,
    blob_key,
    default_principal,
    document_id_for,
)
from app.review.deletion import process_erasures
from app.review.feedback import FeedbackService
from app.review.interfaces import DeletionBlocked
from app.review.retention import RetentionSettings, retention_settings
from app.review.service import build_service
from app.store import migrate
from app.store.deletion import MemoryDeletionRepo, PostgresDeletionRepo
from app.store.files import LocalFileStore
from app.store.memory import memory_store
from app.store.s3 import S3FileStore, file_store_from_config
from tests.test_review_runner import FakeJudge, StubLimiter
from tests.test_store_files_migrate import FakeDb

APP_CONFIG = load_yaml(CONFIGS / "app.yaml")
LOCAL_FILE_CONFIG = {**APP_CONFIG, "files": {**APP_CONFIG.get("files", {}), "backend": "local"}}
NOW = datetime(2026, 11, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _masking_off():
    """The boundary is process-wide; every test starts and ends with masking off."""
    masking.configure(None)
    yield
    masking.configure(None)


def _app(settings=None):
    store = memory_store()
    service = build_service(store, FakeJudge(), StubLimiter())
    application = create_app(
        store=store,
        service=service,
        statutes={},
        feedback=FeedbackService(store, None),
        settings=settings or {},
    )
    return application, store, service


def _document(document_id="d" * 64, **kwargs) -> Document:
    return Document(
        document_id=document_id,
        filename="agb.pdf",
        source_type="pdf",
        parser_version="p1",
        splitter_version="s1",
        text_hash="t",
        **kwargs,
    )


# --- 9.A masking boundary -------------------------------------------------------------


class EmailMasker:
    """A stand-in masker: e-mail addresses only, numbered per text like the real one."""

    PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

    def mask(self, text: str) -> MaskedText:
        found: dict[str, str] = {}

        def replace(match: re.Match) -> str:
            original = match.group(0)
            for placeholder, value in found.items():
                if value == original:
                    return placeholder
            placeholder = masking.placeholder("EMAIL", len(found) + 1)
            found[placeholder] = original
            return placeholder

        return MaskedText(self.PATTERN.sub(replace, text), found)


class StrictDecisions:
    """A fake Gemini Decisions client that refuses unmasked input, as every Wave 1 test
    client must: it checks the payload exactly where the real SDK would send it."""

    def __init__(self) -> None:
        self.received: list[str] = []

    def create(self, *, model, input, questions):  # noqa: A002 - the SDK's own name
        boundary().require_masked(input)
        self.received.append(input)
        return type("Result", (), {"answers": {}, "usage": None})()


class StrictClient:
    def __init__(self) -> None:
        self.decisions = StrictDecisions()


CLAUSE = "Ansprechpartner ist anna.mueller@example.de; Rückfragen an anna.mueller@example.de."


def test_masked_text_is_a_str_that_knows_its_way_back():
    masked = EmailMasker().mask(CLAUSE)

    assert isinstance(masked, str) and isinstance(masked, MaskedText)
    assert "@" not in masked and masked.count("<EMAIL_1>") == 2
    assert masked.placeholders == {"<EMAIL_1>": "anna.mueller@example.de"}
    assert masked.unmask() == CLAUSE
    # A model's answer quoting the placeholder is restored for display; unknown ones stay.
    assert masked.unmask("Bitte <EMAIL_1> und <PERSON_9> fragen.") == (
        "Bitte anna.mueller@example.de und <PERSON_9> fragen."
    )
    # Anything built after masking is a plain str again: unmasked by type.
    assert type(masked + "!") is str and type(masked[:5]) is str


def test_a_strict_client_refuses_unmasked_input_once_masking_is_enabled():
    client = StrictClient()
    # Masking off (the default until 9.A's gate): plain text passes, as before Phase 9.
    client.decisions.create(model="m", input=CLAUSE, questions={})

    masking.configure(EmailMasker())
    with pytest.raises(UnmaskedPayload):
        client.decisions.create(model="m", input=CLAUSE, questions={})
    with pytest.raises(UnmaskedPayload):
        boundary().require_masked(["schon maskiert?", boundary().outbound(CLAUSE)])

    sent = boundary().outbound(CLAUSE)
    client.decisions.create(model="m", input=sent, questions={})
    assert client.decisions.received[-1] == sent and "@" not in client.decisions.received[-1]


def test_the_boundary_passes_text_through_while_disabled_and_never_masks_twice():
    assert not boundary().enabled and boundary().outbound(CLAUSE) is CLAUSE

    masking.configure(EmailMasker())
    once = boundary().outbound(CLAUSE)
    assert boundary().outbound(once) is once
    assert isinstance(IdentityMasker().mask("x"), MaskedText)


def test_a_failing_masker_blocks_the_send():
    class Broken:
        def mask(self, text):
            raise ValueError("model not loaded")

    class Unmarked:
        def mask(self, text):
            return text  # a plain str: not proof of masking

    masking.configure(Broken())
    with pytest.raises(MaskingError, match="model not loaded"):
        boundary().outbound(CLAUSE)
    masking.configure(Unmarked())
    with pytest.raises(MaskingError, match="unmarked"):
        boundary().outbound(CLAUSE)


def test_the_presidio_masker_and_the_langfuse_hook_mask_when_enabled():
    # Wave 1 (masking) implemented both; tests/test_masking.py covers them in depth.
    payload = {"input": [CLAUSE], "metadata": {"review_id": "r"}}
    assert boundary().langfuse_mask(data=payload) is payload  # off: unchanged

    masking.configure(PresidioMasker(allow_regex_only=True))
    assert "anna.mueller@example.de" not in boundary().outbound(CLAUSE)
    assert "anna.mueller@example.de" not in str(boundary().langfuse_mask(data=payload))


def test_masking_is_off_in_the_shipped_config_and_presidio_when_on():
    assert masker_from_config(APP_CONFIG) is None
    assert masker_from_config({}) is None
    masker = masker_from_config(
        {"privacy": {"masking": {"enabled": True, "allow_list": ["BGB"], "entities": ["IBAN"]}}}
    )
    assert isinstance(masker, PresidioMasker)
    assert masker.allow_list == ("BGB",) and masker.entities == ("IBAN",)


# --- 9.E owner model --------------------------------------------------------------------


def test_documents_and_reviews_belong_to_the_default_owner_unless_told_otherwise():
    document = _document()
    review = Review(
        review_id="r",
        document_id=document.document_id,
        agent_version="v",
        scope=[],
        status="queued",
    )

    assert document.owner_id == review.owner_id == DEFAULT_OWNER_ID == "local"
    assert document.source_hash == document.document_id
    assert blob_key(document) == f"{document.document_id}.pdf"
    text = _document(document_id="e" * 64, source_hash="f" * 64)
    text.source_type = "text"
    assert blob_key(text) == f"{'f' * 64}.txt"


def test_document_ids_are_per_owner_and_stable_for_the_default_owner():
    source = "a" * 64

    assert document_id_for(source) == source  # every pre-Phase 9 id stays valid
    anna, ben = document_id_for(source, "anna"), document_id_for(source, "ben")
    assert anna != ben and anna != source and len(anna) == 64
    assert document_id_for(source, "anna") == anna


def test_principal_gives_the_owner_and_the_audit_trail_reviewer():
    anna = Principal(subject="sub-123", issuer="http://kc/realms/agb", name="Anna Müller")
    anna.authenticated = True

    assert anna.owner_id == "sub-123"
    assert anna.reviewer().name == "Anna Müller" and anna.reviewer().authenticated
    local = default_principal()
    assert local.owner_id == DEFAULT_OWNER_ID and not local.authenticated
    assert local.reviewer().name == DEFAULT_OWNER_ID


def test_the_document_list_filters_by_owner():
    store = memory_store()
    for document_id, owner in (("a", "anna"), ("b", "ben"), ("c", DEFAULT_OWNER_ID)):
        store.documents.save(_document(document_id, owner_id=owner), [], {})

    assert {d.document_id for d in store.documents.list()} == {"a", "b", "c"}
    assert [d.document_id for d in store.documents.list(owner_id="anna")] == ["a"]
    assert store.documents.get("b").owner_id == "ben"


def test_migrations_see_the_configured_default_owner():
    db = FakeDb()
    calls: list[tuple] = []
    execute = db.execute

    def recording(sql, params=None):
        if "set_config" in sql:
            calls.append(params)
        return execute(sql, params)

    db.execute = recording
    applied = migrate.apply_pending(db.connect, default_owner="kanzlei")

    assert "006_owners_deletion" in applied
    assert calls == [("kanzlei",)] * len(applied)
    sql = (migrate.MIGRATIONS / "006_owners_deletion.sql").read_text()
    assert "current_setting('agb.default_owner', true)" in sql
    assert "current_setting('agb.erasure', true) = 'on'" in sql


# --- 9.B deletion and 9.C retention -----------------------------------------------------


def test_deletion_models_round_trip():
    tombstone = Tombstone(
        tombstone_id="t1",
        kind="document",
        subject_id="d",
        owner_id="local",
        reason="user",
        removed={"documents": 1, "decision_events": 3},
        at=NOW,
    )
    report = DeletionReport(
        document_id="d",
        review_ids=["r"],
        removed={"documents": 1},
        kept_shared={"clause_texts": 2},
        erasures=[ErasureTask(task_id=1, target="blob", key="d.pdf", requested_at=NOW)],
        tombstone=tombstone,
    )

    assert DeletionReport.model_validate_json(report.model_dump_json()) == report
    assert set(report.removed) <= set(DELETION_TABLES)
    assert SweepReport(started_at=NOW, finished_at=NOW).erasures_failed == 0
    assert DeletionOut(report=report, pending_erasures=1).model_dump()["pending_erasures"] == 1


def test_every_store_has_a_deletion_repo_that_refuses_an_unknown_document():
    # Wave 1 (deletion) replaced the Wave 0 NotImplementedError assertions with behaviour;
    # the full deletion is covered by tests/test_deletion*.py.
    store = memory_store()
    assert isinstance(store.deletion, MemoryDeletionRepo) and store.deletion.store is store
    principal = default_principal()
    with pytest.raises(KeyError):
        store.deletion.delete_document("d", deleted_by=principal, reason="user")
    with pytest.raises(KeyError):
        store.deletion.plan_document("d")
    assert store.deletion.expired("files", NOW) == []
    assert store.deletion.tombstones() == [] and store.deletion.pending_erasures() == []
    postgres = PostgresDeletionRepo(connect=None)
    for name in (
        "plan_document",
        "delete_document",
        "delete_review",
        "sweep_orphans",
        "expired",
        "pending_erasures",
        "finish_erasure",
        "queue_erasures",
        "tombstones",
    ):
        assert callable(getattr(postgres, name))


def test_service_deletion_and_sweep_work_on_an_empty_store():
    _application, _store, service = _app()

    with pytest.raises(KeyError):
        service.delete_document("d", default_principal())
    report = service.sweep(NOW)
    assert report.expired_documents == report.expired_reviews == report.expired_traces == 0
    assert process_erasures(service) == (0, 0)
    assert service.retention == RetentionSettings() and not service.retention.enabled


def test_the_delete_route_answers_404_for_an_unknown_document_and_cors_allows_delete():
    application, _store, _service = _app({"api": {"cors_origins": ["http://localhost:5173"]}})
    with TestClient(application) as client:
        deleted = client.delete("/api/v1/documents/d")
        planned = client.delete("/api/v1/documents/d?dry_run=true")
        preflight = client.options(
            "/api/v1/documents/d",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "DELETE",
            },
        )

    for response in (deleted, planned):
        assert response.status_code == 404 and response.json()["code"] == "not_found"
    assert "DELETE" in preflight.headers["access-control-allow-methods"]


def test_the_delete_route_maps_the_report_404_and_409():
    application, store, service = _app()
    # The route checks the owner before the service (9.E); "missing" stays unknown.
    for document_id in ("d", "busy"):
        store.documents.save(_document(document_id), [], {})
    seen = {}

    def delete_document(document_id, principal, *, dry_run=False):
        seen.update(principal=principal, dry_run=dry_run)
        if document_id == "missing":
            raise KeyError(document_id)
        if document_id == "busy":
            raise DeletionBlocked(["r1"])
        return DeletionReport(
            document_id=document_id,
            dry_run=dry_run,
            removed={"documents": 1},
            erasures=[
                ErasureTask(task_id=1, target="blob", key="d.pdf", requested_at=NOW, done_at=NOW),
                ErasureTask(task_id=2, target="trace", key="tr", requested_at=NOW),
            ],
        )

    service.delete_document = delete_document
    with TestClient(application) as client:
        ok = client.delete("/api/v1/documents/d?dry_run=true")
        missing = client.delete("/api/v1/documents/missing")
        busy = client.delete("/api/v1/documents/busy")

    assert ok.status_code == 200
    assert ok.json()["pending_erasures"] == 1 and ok.json()["report"]["dry_run"] is True
    assert seen["principal"] == default_principal() and seen["dry_run"] is False
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    assert busy.status_code == 409
    assert busy.json()["code"] == "deletion_blocked" and busy.json()["review_ids"] == ["r1"]


def test_retention_settings_default_off_and_validate_periods():
    shipped = retention_settings(APP_CONFIG)
    assert shipped.enabled is False and shipped.files_days is None
    assert shipped.traces_days == 30
    assert retention_settings({}) == RetentionSettings()
    with pytest.raises(ValueError, match="at least 1"):
        retention_settings({"retention": {"reviews_days": 0}})


# --- 9.D storage ---------------------------------------------------------------------


def test_the_memory_file_store_deletes_idempotently_and_does_not_sign():
    files = memory_store().files
    files.put("k.pdf", b"x")

    assert files.presign("k.pdf", 60) is None
    assert files.delete("k.pdf") is True and files.delete("k.pdf") is False
    assert not files.exists("k.pdf") and files.keys() == []


def test_the_file_store_is_chosen_by_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "")
    monkeypatch.setenv("AWS_REGION", "")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret")
    assert isinstance(file_store_from_config(LOCAL_FILE_CONFIG, tmp_path), LocalFileStore)
    assert (tmp_path / "stores/files").is_dir()
    s3 = file_store_from_config(
        {"files": {"backend": "s3", "s3": {"bucket": "b", "region": "fr-par"}}}, tmp_path
    )
    assert isinstance(s3, S3FileStore)
    assert s3.settings.bucket == "b" and s3.settings.region == "fr-par"
    assert s3.settings.server_side_encryption == "AES256" and s3.settings.download == "proxy"
    with pytest.raises(ValueError, match="files.backend"):
        file_store_from_config({"files": {"backend": "ftp"}}, tmp_path)


# --- 9.E login ------------------------------------------------------------------------


def test_me_is_the_default_owner_while_login_is_off_and_401_without_a_token_when_on():
    application, _store, _service = _app({"auth": {"default_owner": "kanzlei"}})
    with TestClient(application) as client:
        off = client.get("/api/v1/me")
    application, _store, _service = _app({"auth": {"enabled": True}})
    with TestClient(application) as client:
        on = client.get("/api/v1/me")

    assert off.status_code == 200
    assert off.json() == {
        "owner_id": "kanzlei",
        "name": None,
        "roles": [],
        "authenticated": False,
        "auth_enabled": False,
    }
    # Wave 2 wrote the login: no bearer token is 401, never the default owner.
    assert on.status_code == 401 and on.json()["code"] == "unauthenticated"
    assert on.headers["www-authenticate"].startswith("Bearer")

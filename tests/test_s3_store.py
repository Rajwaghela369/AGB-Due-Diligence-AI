"""S3 FileStore tests use botocore's wire-level stubber, never an object-store service."""

from io import BytesIO

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app.store.files import LocalFileStore
from app.store.s3 import S3FileStore, S3Settings, file_store_from_config
from scripts.migrate_files_to_s3 import migrate


def _client():
    return boto3.client(
        "s3",
        endpoint_url="http://s3.invalid",
        region_name="eu-central-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def _store(client):
    return S3FileStore(S3Settings(bucket="files", prefix="source/"), client=client)


def test_put_is_immutable_and_requests_server_side_encryption():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "put_object",
            {},
            {
                "Bucket": "files",
                "Key": "source/a.pdf",
                "Body": ANY,
                "IfNoneMatch": "*",
                "ServerSideEncryption": "AES256",
            },
        )
        assert store.put("a.pdf", b"contents") == "a.pdf"


def test_existing_put_is_a_noop_and_missing_values_follow_file_contract():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="PreconditionFailed", http_status_code=412
        )
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"contents"), 8)},
            {"Bucket": "files", "Key": "source/a.pdf"},
        )
        stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stubber.add_response("delete_object", {}, {"Bucket": "files", "Key": "source/missing.pdf"})

        assert store.put("a.pdf", b"contents") == "a.pdf"
        with pytest.raises(FileNotFoundError):
            store.get("missing.pdf")
        assert store.exists("missing.pdf") is False
        # Delete is a bare DeleteObject: idempotent, and it never needs HEAD or ListBucket.
        assert store.delete("missing.pdf") is True


def test_concurrent_conditional_put_is_a_noop_only_when_the_blob_matches():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "put_object", service_error_code="ConditionalRequestConflict", http_status_code=409
        )
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"contents"), 8)},
            {"Bucket": "files", "Key": "source/a.pdf"},
        )

        assert store.put("a.pdf", b"contents") == "a.pdf"


def test_delete_does_not_head_an_absent_key_without_list_bucket_permission():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_response("delete_object", {}, {"Bucket": "files", "Key": "source/missing.pdf"})

        assert store.delete("missing.pdf") is True


def test_get_delete_and_presign_use_the_configured_prefix():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "get_object",
            {"Body": StreamingBody(BytesIO(b"contents"), 8)},
            {"Bucket": "files", "Key": "source/a.pdf"},
        )
        stubber.add_response("delete_object", {}, {"Bucket": "files", "Key": "source/a.pdf"})

        assert store.get("a.pdf") == b"contents"
        assert store.delete("a.pdf") is True

    client.generate_presigned_url = lambda *args, **kwargs: "https://signed.invalid/a.pdf"
    assert store.presign("a.pdf", 60) == "https://signed.invalid/a.pdf"
    with pytest.raises(ValueError, match="between"):
        store.presign("a.pdf", 0)
    with pytest.raises(ValueError, match="between"):
        store.presign("a.pdf", 3601)


@pytest.mark.parametrize("key", ["", "/a.pdf", "../a.pdf", "a//b.pdf", "a/../b.pdf"])
def test_s3_keys_cannot_escape_the_configured_prefix(key):
    with pytest.raises(ValueError):
        _store(_client()).exists(key)


def test_factory_reads_credentials_only_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "from-env")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret-from-env")
    store = file_store_from_config({"files": {"backend": "s3"}}, tmp_path)

    assert store._access_key_id == "from-env"
    assert store._secret_access_key == "secret-from-env"


@pytest.mark.parametrize("missing", ["S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"])
def test_s3_backend_requires_explicit_credentials(monkeypatch, tmp_path, missing):
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "present")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "present")
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match=missing):
        file_store_from_config({"files": {"backend": "s3"}}, tmp_path)


@pytest.mark.parametrize(
    ("s3", "message"),
    [
        ({"server_side_encryption": "none"}, "server_side_encryption"),
        ({"download": "public"}, "download"),
        ({"presign_seconds": 0}, "presign_seconds"),
        ({"presign_seconds": 3601}, "presign_seconds"),
    ],
)
def test_s3_config_rejects_unsafe_or_invalid_values(s3, message, tmp_path):
    with pytest.raises(ValueError, match=message):
        file_store_from_config({"files": {"backend": "s3", "s3": s3}}, tmp_path)


def test_migration_copies_verifies_and_is_resumable(tmp_path):
    source = LocalFileStore(tmp_path / "source")
    source.put("a.pdf", b"alpha")

    class Destination:
        def __init__(self):
            self.blobs = {}

        def exists(self, key):
            return key in self.blobs

        def put(self, key, data):
            self.blobs.setdefault(key, data)
            return key

        def get(self, key):
            return self.blobs[key]

        class settings:
            server_side_encryption = "AES256"

        def encryption(self, key):
            return self.settings.server_side_encryption

    destination = Destination()
    assert migrate(source, destination) == (1, 0)
    assert migrate(source, destination) == (0, 1)


def test_migration_skips_crash_leftover_temp_files_and_checks_encryption(tmp_path):
    source = LocalFileStore(tmp_path / "source")
    source.put("a.pdf", b"alpha")
    (source.root / ".tmp-interrupted").write_bytes(b"partial")

    class Destination:
        settings = type("Settings", (), {"server_side_encryption": "AES256"})()

        def __init__(self):
            self.blobs = {}

        def exists(self, key):
            return key in self.blobs

        def put(self, key, data):
            self.blobs[key] = data
            return key

        def get(self, key):
            return self.blobs[key]

        def encryption(self, key):
            return self.settings.server_side_encryption

    destination = Destination()
    assert migrate(source, destination) == (1, 0)
    assert destination.blobs == {"a.pdf": b"alpha"}


def test_list_keys_pages_through_list_objects_v2_inside_the_prefix():
    from datetime import UTC, datetime

    client = _client()
    store = _store(client)
    first = datetime(2026, 11, 1, tzinfo=UTC)
    second = datetime(2026, 11, 2, tzinfo=UTC)
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "source/a.pdf", "LastModified": first}],
                "IsTruncated": True,
                "NextContinuationToken": "page-2",
            },
            {"Bucket": "files", "Prefix": "source/"},
        )
        stubber.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "source/b.txt", "LastModified": second}], "IsTruncated": False},
            {"Bucket": "files", "Prefix": "source/", "ContinuationToken": "page-2"},
        )
        assert store.list_keys() == [("a.pdf", first), ("b.txt", second)]
        stubber.assert_no_pending_responses()


def test_list_keys_of_an_empty_bucket_and_with_a_key_prefix():
    client = _client()
    store = _store(client)
    with Stubber(client) as stubber:
        stubber.add_response(
            "list_objects_v2", {"IsTruncated": False}, {"Bucket": "files", "Prefix": "source/a"}
        )
        assert store.list_keys("a") == []


def test_local_list_keys_skips_temp_files_and_reports_modification_times(tmp_path):
    store = LocalFileStore(tmp_path)
    store.put("a.pdf", b"a")
    store.put("nested/b.txt", b"b")
    (tmp_path / ".tmp-123").write_bytes(b"half")

    listed = store.list_keys()

    assert [key for key, _ in listed] == ["a.pdf", "nested/b.txt"]
    assert all(modified.tzinfo is not None for _, modified in listed)
    assert [key for key, _ in store.list_keys("nested/")] == ["nested/b.txt"]

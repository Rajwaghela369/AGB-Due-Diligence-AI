"""S3-compatible object storage for source files.

One implementation serves any compatible provider. The endpoint, region, bucket, and
encryption settings come from `files.s3`; provider-specific features are not required.

The contract (`app.store.interfaces.FileStore`):

- `put` is a conditional write (`If-None-Match: *`), so the first writer's blob stands, as
  on local disk; every write asks for server-side encryption (`server_side_encryption`).
- `get` raises FileNotFoundError for a missing key (S3's 404 / `NoSuchKey`).
- `delete` is idempotent: True if the key existed.
- `presign` returns a GET URL valid for `expires_in` seconds; the API redirects to it or
  proxies the bytes (`files.s3.download`), see docs/Phase 9 API.md.

Credentials come from `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`, never from YAML.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from botocore.exceptions import ClientError

from app.store.interfaces import FileStore

FilesBackend = Literal["local", "s3"]
MAX_PRESIGN_SECONDS = 3600


def validate_presign_seconds(value: int) -> int:
    """Keep bearer download URLs short enough to revoke access promptly."""
    if not 1 <= value <= MAX_PRESIGN_SECONDS:
        raise ValueError(f"files.s3.presign_seconds must be between 1 and {MAX_PRESIGN_SECONDS}")
    return value


@dataclass(frozen=True)
class S3Settings:
    """`files.s3` in configs/app.yaml."""

    endpoint_url: str = "http://localhost:9000"  # MinIO in Compose
    region: str = "eu-central-1"
    bucket: str = "agb-check-files"
    prefix: str = ""  # prepended to every key, e.g. "prod/"
    server_side_encryption: str = "AES256"  # or "aws:kms" where the provider offers it
    presign_seconds: int = 300
    # "proxy": the API streams the bytes (the browser never talks to the bucket);
    # "redirect": the API answers 307 to a presigned URL.
    download: Literal["proxy", "redirect"] = "proxy"

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> S3Settings:
        block = ((config.get("files") or {}).get("s3")) or {}
        defaults = cls()
        settings = cls(
            endpoint_url=str(block.get("endpoint_url", defaults.endpoint_url)),
            region=str(block.get("region", defaults.region)),
            bucket=str(block.get("bucket", defaults.bucket)),
            prefix=str(block.get("prefix", defaults.prefix) or ""),
            server_side_encryption=str(
                block.get("server_side_encryption", defaults.server_side_encryption)
            ),
            presign_seconds=int(block.get("presign_seconds", defaults.presign_seconds)),
            download=cast(Literal["proxy", "redirect"], block.get("download", defaults.download)),
        )
        if settings.server_side_encryption not in {"AES256", "aws:kms"}:
            raise ValueError("files.s3.server_side_encryption must be 'AES256' or 'aws:kms'")
        if settings.download not in {"proxy", "redirect"}:
            raise ValueError("files.s3.download must be 'proxy' or 'redirect'")
        validate_presign_seconds(settings.presign_seconds)
        return settings


class S3FileStore:
    """`FileStore` on an S3-compatible bucket."""

    def __init__(
        self,
        settings: S3Settings,
        *,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        client: Any = None,  # an injected S3 client (tests: a fake, or MinIO in Compose)
    ) -> None:
        self.settings = settings
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client

    def _object_key(self, key: str) -> str:
        """Join a configured namespace without allowing a caller to escape it."""
        if (
            not key
            or key.startswith("/")
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ValueError(f"invalid file key: {key!r}")
        prefix = self.settings.prefix.strip("/")
        return f"{prefix}/{key}" if prefix else key

    @property
    def _s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.endpoint_url,
                region_name=self.settings.region,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
            )
        return self._client

    @staticmethod
    def _missing(exc: ClientError) -> bool:
        return str(exc.response.get("Error", {}).get("Code", "")) in {
            "404",
            "NoSuchKey",
            "NotFound",
        }

    @staticmethod
    def _conditional_conflict(exc: ClientError) -> bool:
        return str(exc.response.get("Error", {}).get("Code", "")) in {
            "ConditionalRequestConflict",
            "PreconditionFailed",
            "409",
            "412",
        }

    def put(self, key: str, data: bytes) -> str:
        try:
            self._s3.put_object(
                Bucket=self.settings.bucket,
                Key=self._object_key(key),
                Body=data,
                IfNoneMatch="*",
                ServerSideEncryption=self.settings.server_side_encryption,
            )
        except ClientError as exc:
            if not self._conditional_conflict(exc):
                raise
            # Both a normal immutable re-upload (412) and two writers racing (409) are
            # successful only if the winner really stored the same content.
            if hashlib.sha256(self.get(key)).digest() != hashlib.sha256(data).digest():
                raise RuntimeError(f"immutable object conflict for {key!r}") from exc
        return key

    def get(self, key: str) -> bytes:
        try:
            response = self._s3.get_object(Bucket=self.settings.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if self._missing(exc):
                raise FileNotFoundError(key) from exc
            raise
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.settings.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if self._missing(exc):
                return False
            raise
        return True

    def delete(self, key: str) -> bool:
        try:
            self._s3.delete_object(Bucket=self.settings.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if self._missing(exc):
                return False
            raise
        # S3 delete is idempotent and does not require ListBucket permission. AWS returns
        # 204 for an absent key too, so it cannot distinguish that case without a HEAD.
        return True

    def encryption(self, key: str) -> str | None:
        """Return S3's recorded encryption mode for migration verification."""
        response = self._s3.head_object(Bucket=self.settings.bucket, Key=self._object_key(key))
        value = response.get("ServerSideEncryption")
        return str(value) if value is not None else None

    def presign(self, key: str, expires_in: int) -> str | None:
        validate_presign_seconds(expires_in)
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.settings.bucket, "Key": self._object_key(key)},
            ExpiresIn=expires_in,
        )

    def list_keys(self, prefix: str = "") -> list[tuple[str, datetime]]:
        """ListObjectsV2, paged, inside the configured namespace; keys come back without
        `settings.prefix`, as the other methods take them."""
        namespace = self.settings.prefix.strip("/")
        root = f"{namespace}/" if namespace else ""
        found: list[tuple[str, datetime]] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.settings.bucket, Prefix=root + prefix):
            for item in page.get("Contents", []):
                key = str(item["Key"])[len(root) :]
                modified = item["LastModified"]
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=UTC)
                found.append((key, modified))
        return found


def file_store_from_config(config: Mapping[str, Any], root: Path) -> FileStore:
    """The FileStore `files.backend` names: "local" (default) under `root / files.root`, or
    "s3" with `files.s3`. Raises ValueError for an unknown backend."""
    from app.store.files import LocalFileStore

    block = config.get("files") or {}
    backend = block.get("backend", "local")
    if backend == "local":
        return LocalFileStore(root / str(block.get("root", "stores/files")))
    if backend == "s3":
        from core.settings import load_settings

        settings = load_settings()
        s3_settings = S3Settings.from_config(config)
        if settings.aws_endpoint_url_s3 or settings.aws_region:
            s3_settings = replace(
                s3_settings,
                endpoint_url=settings.aws_endpoint_url_s3 or s3_settings.endpoint_url,
                region=settings.aws_region or s3_settings.region,
            )
        if not settings.s3_access_key_id or not settings.s3_secret_access_key:
            raise ValueError(
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY (or S3_ACCESS_KEY_ID/"
                "S3_SECRET_ACCESS_KEY) must be set when files.backend is 's3'"
            )
        return S3FileStore(
            s3_settings,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
        )
    raise ValueError(f"unknown files.backend {backend!r}; expected 'local' or 's3'")

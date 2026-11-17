"""S3-compatible EU object storage for source files (Phase 9, 9.D).

One implementation for every S3-compatible provider: MinIO in Docker Compose for
development and tests, and an EU provider (Hetzner, Scaleway or OVH) in production, chosen
only by `files.s3.endpoint_url` and `files.s3.region` (decision #3). Nothing here may
depend on a provider's extras, so moving provider is a configuration change.

The contract (`app.store.interfaces.FileStore`):

- `put` is a conditional write (`If-None-Match: *`), so the first writer's blob stands, as
  on local disk; every write asks for server-side encryption (`server_side_encryption`).
- `get` raises FileNotFoundError for a missing key (S3's 404 / `NoSuchKey`).
- `delete` is idempotent: True if the key existed.
- `presign` returns a GET URL valid for `expires_in` seconds; the API redirects to it or
  proxies the bytes (`files.s3.download`), see docs/Phase 9 API.md.

Credentials come from the environment (`S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`), never
from configs/app.yaml. Wave 0 ships the settings and a stub; the storage slice (Wave 1)
writes the client (boto3, or the minio client) and `scripts/migrate_files_to_s3.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_TODO = "Phase 9 Wave 1: S3FileStore"

FilesBackend = Literal["local", "s3"]


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
        return cls(
            endpoint_url=str(block.get("endpoint_url", defaults.endpoint_url)),
            region=str(block.get("region", defaults.region)),
            bucket=str(block.get("bucket", defaults.bucket)),
            prefix=str(block.get("prefix", defaults.prefix) or ""),
            server_side_encryption=str(
                block.get("server_side_encryption", defaults.server_side_encryption)
            ),
            presign_seconds=int(block.get("presign_seconds", defaults.presign_seconds)),
            download=block.get("download", defaults.download),
        )


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

    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError(_TODO)

    def get(self, key: str) -> bytes:
        raise NotImplementedError(_TODO)

    def exists(self, key: str) -> bool:
        raise NotImplementedError(_TODO)

    def delete(self, key: str) -> bool:
        raise NotImplementedError(_TODO)

    def presign(self, key: str, expires_in: int) -> str | None:
        raise NotImplementedError(_TODO)


def file_store_from_config(config: Mapping[str, Any], root: Path) -> Any:
    """The FileStore `files.backend` names: "local" (default) under `root / files.root`, or
    "s3" with `files.s3`. Raises ValueError for an unknown backend."""
    from app.store.files import LocalFileStore

    block = config.get("files") or {}
    backend = block.get("backend", "local")
    if backend == "local":
        return LocalFileStore(root / str(block.get("root", "stores/files")))
    if backend == "s3":
        return S3FileStore(S3Settings.from_config(config))
    raise ValueError(f"unknown files.backend {backend!r}; expected 'local' or 's3'")

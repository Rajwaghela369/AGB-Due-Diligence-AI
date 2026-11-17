"""Copy immutable local blobs into S3 without making the source disk disposable.

The script verifies every copied object by byte count and SHA-256. It deliberately leaves
the local file alone: changing `files.backend` is a separate, reversible deployment step.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from core.config import load_yaml
from core.paths import CONFIGS, ROOT

from app.store.files import LocalFileStore
from app.store.s3 import S3FileStore, file_store_from_config


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def migrate(source: LocalFileStore, destination: S3FileStore) -> tuple[int, int]:
    """Copy absent blobs, then verify all source blobs; return (copied, already_present)."""
    copied = 0
    already_present = 0
    for path in sorted(
        item for item in source.root.rglob("*") if item.is_file() and not item.name.startswith(".tmp-")
    ):
        key = path.relative_to(source.root).as_posix()
        expected = source.get(key)
        if destination.exists(key):
            already_present += 1
        else:
            destination.put(key, expected)
            if destination.encryption(key) != destination.settings.server_side_encryption:
                raise RuntimeError(f"encryption verification failed for {key!r}")
            copied += 1
        actual = destination.get(key)
        if len(actual) != len(expected) or _digest(actual) != _digest(expected):
            raise RuntimeError(f"verification failed for {key!r}")
    return copied, already_present


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--config", type=Path, default=CONFIGS / "app.yaml")
    args = parser.parse_args()
    config = load_yaml(args.config)
    files = config.get("files") or {}
    source = LocalFileStore(args.root / str(files.get("root", "stores/files")))
    destination = file_store_from_config({"files": {**files, "backend": "s3"}}, args.root)
    if not isinstance(destination, S3FileStore):  # defensive: config construction above fixes this
        raise RuntimeError("S3 destination was not configured")
    copied, already_present = migrate(source, destination)
    print(f"copied={copied} already_present={already_present}")


if __name__ == "__main__":
    main()

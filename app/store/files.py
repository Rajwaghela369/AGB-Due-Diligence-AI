"""Local-disk FileStore: content-addressed blobs under one root.

Keys are chosen by the caller (the document id for a PDF), so a blob is
immutable: `put` of an existing key changes nothing. Writes go to a temp file
in the same directory and are renamed into place, so a reader or a crash never
sees a half-written file. Phase 9 adds EU object storage (`app.store.s3`) behind the
same protocol, chosen by `files.backend`; `delete` and `presign` are Phase 9 Wave 1.
"""

import os
import tempfile
from contextlib import suppress
from pathlib import Path

DEFAULT_ROOT = Path("stores/files")


class LocalFileStore:
    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not key or not path.is_relative_to(self.root.resolve()) or path == self.root.resolve():
            raise ValueError(f"invalid file key: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # link fails if the key exists, unlike rename: the first writer's blob stands.
            with suppress(FileExistsError):
                os.link(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        raise NotImplementedError("Phase 9 Wave 1: LocalFileStore.delete")

    def presign(self, key: str, expires_in: int) -> str | None:
        """Local disk cannot sign a URL: always None, and the API proxies the bytes."""
        raise NotImplementedError("Phase 9 Wave 1: LocalFileStore.presign")

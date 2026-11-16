"""Numbered SQL migrations, applied in order and recorded in `schema_migrations`.

Each file runs in its own transaction together with its bookkeeping row, so a
failed migration leaves no half-applied schema and the next start retries it.
An advisory lock serialises two processes starting at once (the API and a
worker, or two replicas): the second waits, then finds the file already applied.

Works on an empty database and on the current Neon one: `001` moves the old
evaluation `verdicts` table aside only if it exists.
"""

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from core.db import connection

MIGRATIONS = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{3,})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 7_315_001  # arbitrary, fixed: "agb-check migrations"

_BOOKKEEPING = """
create table if not exists schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
)
"""


def migration_files(directory: Path = MIGRATIONS) -> list[Path]:
    """Migration files in numeric order; a file that is not `NNN_name.sql` is an error."""
    files = sorted(directory.glob("*.sql"))
    for f in files:
        if not _NAME.match(f.name):
            raise ValueError(f"migration {f.name!r} must be named NNN_snake_case.sql")
    return files


def apply_pending(
    connect: Callable[[], AbstractContextManager[Any]] = connection,
    directory: Path = MIGRATIONS,
) -> list[str]:
    """Apply every migration not yet recorded; returns the versions applied now."""
    with connect() as conn:
        # Lock first: two first-time starters must not both create the bookkeeping table.
        conn.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute(_BOOKKEEPING)

    applied: list[str] = []
    for path in migration_files(directory):
        with connect() as conn:
            conn.execute("select pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            done = conn.execute(
                "select 1 from schema_migrations where version = %s", (path.stem,)
            ).fetchone()
            if done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into schema_migrations (version) values (%s)", (path.stem,))
            applied.append(path.stem)
    return applied

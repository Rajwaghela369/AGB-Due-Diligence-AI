"""Neon Postgres access. The only module that opens a connection."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from core.settings import load_settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Open a connection, commit on success, roll back on failure."""
    url = load_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")

    with psycopg.connect(url) as conn:
        yield conn


def ping() -> str:
    """Server version, used by the smoke test to prove reachability."""
    with connection() as conn:
        return conn.execute("select version()").fetchone()[0]

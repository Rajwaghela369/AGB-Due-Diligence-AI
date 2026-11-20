"""LangGraph checkpoint storage for interrupted clauses.

An `uncertain` clause stops its graph after `self_check` (see `app.agent.graph`), and the
lawyer may decide hours and several restarts later, so in production the checkpoint lives
in Postgres next to the review, under thread `{review_id}:{position}`. Tests and the e2e
app use the in-memory saver; the offline evals use none, and their graph never stops.

The saver needs its own `psycopg_pool` pool rather than the store's: LangGraph's Postgres
saver wants autocommit connections with dict rows and no prepared statements (Neon's
pooler cannot keep them), which the store's connections are not. `setup()` creates or
migrates LangGraph's tables once at startup.

Only what the graph state holds is serialised. The one non-builtin type in it is
`core.retrieval.Retrieved`, allowed explicitly so LangGraph's msgpack serde neither warns
nor, once its strict mode becomes the default, refuses to read our own checkpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

# The types the graph state may contain beyond builtins and LangGraph's own.
STATE_TYPES = [("core.retrieval", "Retrieved")]

DEFAULT_POOL_SIZE = 4


def serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


@dataclass
class Checkpointer:
    """A saver for `make_judge(checkpointer=...)` plus whatever must be closed with it."""

    saver: Any
    _pool: Any = field(default=None, repr=False)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


def memory_checkpointer() -> Checkpointer:
    """In-process checkpoints: tests, and a 'restart' that keeps the same saver object."""
    return Checkpointer(InMemorySaver(serde=serde()))


def postgres_checkpointer(url: str | None = None, *, max_size: int = DEFAULT_POOL_SIZE):
    """Checkpoints in Postgres (`DATABASE_URL` unless `url` is given), tables set up."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    if url is None:
        from core.settings import load_settings

        url = load_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set; copy .env.example to .env")
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        # Neon drops idle connections server-side; check one before handing it out.
        check=ConnectionPool.check_connection,
        open=True,
    )
    try:
        saver = PostgresSaver(pool, serde=serde())  # type: ignore[arg-type]
        saver.setup()
    except Exception:
        pool.close()
        raise
    return Checkpointer(saver, pool)

"""Read-only access to the published, label-free clause table."""

from typing import Any

from core.db import connection
from core.invariants import assert_no_labels
from core.schemas import Clause


def load_clauses(split: str | None = None) -> list[Clause]:
    sql = "select clause_id, contract_id, title, text, topics, split from clauses"
    params: tuple[Any, ...] = ()
    if split is not None:
        sql += " where split = %s"
        params = (split,)
    sql += " order by clause_id"
    with connection() as conn:
        columns = [
            row[0]
            for row in conn.execute(
                "select column_name from information_schema.columns where table_name = 'clauses'"
            ).fetchall()
        ]
        assert_no_labels(columns, "clauses table at read")
        rows = conn.execute(sql, params).fetchall()
    return [
        Clause(
            **dict(
                zip(
                    ("clause_id", "contract_id", "title", "text", "topics", "split"),
                    row,
                    strict=True,
                )
            )
        )
        for row in rows
    ]

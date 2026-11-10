"""2.5 Publish: clauses to Postgres without labels, the answer key to Langfuse.

The one boundary in P2 where the label-leak invariant has teeth. The clause
table is what the agent reads, so it must not carry `label_void`; the Langfuse
dataset is what scores the agent, so it must. Both assertions run here, and
`core.invariants` is asserted again by the app's clause loader, so a label
cannot reach the agent by either route.

`dry_run=True` builds and asserts everything without opening a connection or
touching Langfuse, so the invariant can be verified before anything is written.
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd
from core import langfuse_client
from core.db import connection
from core.invariants import assert_no_labels
from core.logging import get_logger

log = get_logger("p2.publish")

# The published shape, matching core.schemas.Clause plus the split the clause
# belongs to. No label column exists by construction - the assertions below
# make that a checked property rather than an intention.
CLAUSE_COLUMNS = ("clause_id", "contract_id", "split", "title", "text", "topics")

DDL = """
create table if not exists {table} (
    clause_id   text primary key,
    contract_id integer not null,
    split       text not null,
    title       text not null,
    text        text not null,
    topics      text[] not null
)
"""

INSERT = """
insert into {table} (clause_id, contract_id, split, title, text, topics)
values (%s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class PublishResult:
    table: str
    table_rows: int
    dataset: str
    dataset_items: int
    dry_run: bool


def compose_title(section_no: object, section_title: object) -> str:
    """One display title from the two parsed halves, either of which can be absent."""
    parts = [str(part) for part in (section_no, section_title) if isinstance(part, str) and part]
    return " ".join(parts)


def build_clause_rows(halves: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Both halves, in the published shape, with the label left behind.

    The dev half is published too: Phase 3 and 4 iterate against it, and the
    app has no business knowing which half a clause came from beyond the
    `split` column that lets evals select one.
    """
    frames = []
    for split, part in halves.items():
        frames.append(
            pd.DataFrame(
                {
                    "clause_id": part.clause_id,
                    "contract_id": part.contract_id.astype(int),
                    "split": split,
                    "title": [
                        compose_title(no, title)
                        for no, title in zip(part.section_no, part.section_title, strict=True)
                    ],
                    "text": part.text,
                    "topics": [list(topics) for topics in part.topics],
                }
            )
        )

    rows = pd.concat(frames, ignore_index=True)[list(CLAUSE_COLUMNS)]
    # Before the insert: nothing label-shaped may be in the frame we are about
    # to write.
    assert_no_labels(list(rows.columns), "clause rows bound for the clauses table")
    return rows


def build_dataset_items(held_out: pd.DataFrame, split: str) -> list[dict[str, Any]]:
    """The answer key: clause in, expected verdict out.

    `input` mirrors exactly what the agent is given, so an experiment cannot
    accidentally feed it something the production path would not have. The
    label lives only in `expected_output`.
    """
    items = []
    for row in held_out.to_dict("records"):
        items.append(
            {
                "id": row["clause_id"],
                "input": {
                    "clause_id": row["clause_id"],
                    "title": compose_title(row["section_no"], row["section_title"]),
                    "text": row["text"],
                    "topics": list(row["topics"]),
                },
                "expected_output": {"label_void": bool(row["label_void"])},
                # Slicing dimensions for error analysis. None of these is a label.
                "metadata": {
                    "split": split,
                    "contract_id": int(row["contract_id"]),
                    "is_fragment": bool(row["is_fragment"]),
                    "foreign_law_choice": bool(row["foreign_law_choice"]),
                },
            }
        )
    return items


def _published_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "select column_name from information_schema.columns where table_name = %s",
        (table,),
    ).fetchall()
    return [name for (name,) in rows]


def publish_clauses(rows: pd.DataFrame, table: str, dry_run: bool) -> int:
    """Rebuild the clause table inside one transaction."""
    if dry_run:
        log.info("dry run: %d clause rows prepared for %s, nothing written", len(rows), table)
        return len(rows)

    payload = [tuple(row) for row in rows.itertuples(index=False, name=None)]
    with connection() as conn:
        conn.execute(DDL.format(table=table))
        # Truncate and reinsert in the same transaction: a failure rolls back to
        # the previous contents rather than leaving the table empty.
        conn.execute(f"truncate {table}")
        conn.cursor().executemany(INSERT.format(table=table), payload)

        # After the insert: assert against the table as the database reports
        # it, not against the frame we just built.
        assert_no_labels(_published_columns(conn, table), f"{table} table in Postgres")

    log.info("published %d clauses to %s", len(rows), table)
    return len(rows)


def publish_dataset(items: list[dict[str, Any]], name: str, dry_run: bool) -> int:
    """Upsert the answer key. Items are keyed by clause_id, so re-runs converge."""
    if dry_run:
        log.info("dry run: %d dataset items prepared for %s, nothing written", len(items), name)
        return len(items)

    client = langfuse_client.client()
    client.create_dataset(
        name=name,
        description=(
            "AGB-DE liability and warranty clauses, held-out split. "
            "expected_output.label_void is the authors' annotation."
        ),
    )
    for item in items:
        client.create_dataset_item(dataset_name=name, **item)
    client.flush()

    log.info("published %d items to Langfuse dataset %s", len(items), name)
    return len(items)


def publish(
    halves: dict[str, pd.DataFrame],
    held_out: str,
    table: str,
    dataset: str,
    dry_run: bool = False,
) -> PublishResult:
    rows = build_clause_rows(halves)
    items = build_dataset_items(halves[held_out], held_out)

    return PublishResult(
        table=table,
        table_rows=publish_clauses(rows, table, dry_run),
        dataset=dataset,
        dataset_items=publish_dataset(items, dataset, dry_run),
        dry_run=dry_run,
    )

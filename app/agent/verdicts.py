"""Verdict storage, keyed by what produced the verdict rather than by when.

The key is `(clause_id, agent_version)`. Running the same agent over the same
clause converges on one row; changing a prompt, a model, the retrieval policy or
the index produces a new version that lands beside the old one. Verdicts are
superseded, never overwritten, and a verdict already reached can be served from
here instead of the model.
"""

from typing import Any

from core.config import config_hash
from core.db import connection
from core.schemas import VerdictRecord

DDL = """
create table if not exists {table} (
    clause_id         text not null,
    agent_version     text not null,
    run_id            text not null,
    verdict           text not null,
    legal_basis       text[] not null,
    reason_de         text not null,
    confidence        double precision not null,
    lawyer_decision   text,
    langfuse_trace_id text,
    primary key (clause_id, agent_version)
)
"""

INSERT = """
insert into {table}
    (clause_id, agent_version, run_id, verdict, legal_basis, reason_de,
     confidence, lawyer_decision, langfuse_trace_id)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
on conflict (clause_id, agent_version) do update set
    run_id = excluded.run_id,
    verdict = excluded.verdict,
    legal_basis = excluded.legal_basis,
    reason_de = excluded.reason_de,
    confidence = excluded.confidence,
    langfuse_trace_id = excluded.langfuse_trace_id
"""
# lawyer_decision is deliberately absent from the DO UPDATE SET: a human's
# ruling outlives any number of agent re-runs over the same clause.

SELECT = """
select run_id, clause_id, verdict, legal_basis, reason_de, confidence,
       lawyer_decision, langfuse_trace_id
from {table} where clause_id = %s and agent_version = %s
"""

_COLUMNS = (
    "run_id",
    "clause_id",
    "verdict",
    "legal_basis",
    "reason_de",
    "confidence",
    "lawyer_decision",
    "langfuse_trace_id",
)


def agent_version(
    app_config: dict[str, Any],
    index_hash: str,
    prompt_fingerprints: dict[str, str],
) -> str:
    """Hash of everything that can change a verdict.

    Same pattern the index already uses for its own lineage, one level up. All
    three inputs are required: a version that silently ignores the index or the
    prompts would serve a stale verdict as current.
    """
    return config_hash(
        {
            "prompts": prompt_fingerprints,
            "models": app_config.get("models", {}),
            "retrieval": app_config.get("retrieval", {}),
            "agent": app_config.get("agent", {}),
            "index": index_hash,
        }
    )


def ensure_table(table: str = "verdicts") -> None:
    """Create the table once per run, from the caller's setup path.

    Deliberately not called by `save`: at concurrency 8 that was one CREATE
    TABLE per clause, which races on Postgres for no benefit.
    """
    with connection() as conn:
        conn.execute(DDL.format(table=table))


def save(record: VerdictRecord, version: str, table: str = "verdicts") -> None:
    with connection() as conn:
        conn.execute(
            INSERT.format(table=table),
            (
                record.clause_id,
                version,
                record.run_id,
                record.verdict,
                record.legal_basis,
                record.reason_de,
                record.confidence,
                record.lawyer_decision,
                record.langfuse_trace_id,
            ),
        )


def get(clause_id: str, version: str, table: str = "verdicts") -> VerdictRecord | None:
    """The cached verdict for this clause under this agent version, if any."""
    with connection() as conn:
        row = conn.execute(SELECT.format(table=table), (clause_id, version)).fetchone()
    if not row:
        return None
    return VerdictRecord(**dict(zip(_COLUMNS, row, strict=True)))

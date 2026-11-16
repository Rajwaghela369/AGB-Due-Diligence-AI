"""Verdict storage for the evaluation harness, keyed by what produced the verdict.

The key is `(content_hash, agent_version)`, the same as the service's `verdicts`
table: the hash of the clause text, not the eval clause id, so the harness and
the product share one cache and a clause judged in an evaluation is not judged
again in a review. Running the same agent over the same text converges on one
row; changing a prompt, a model, the retrieval policy or the index produces a new
version that lands beside the old one. Verdicts are superseded, never overwritten.

The table itself belongs to `app.store.migrations`; this module only reads and
writes it. A human's ruling on a clause is a `decision` on one review, not a
column here.
"""

from typing import Any

from core.config import config_hash
from core.db import connection
from core.schemas import VerdictRecord

from app.domain import ClauseVerdict

INSERT = """
insert into verdicts
    (content_hash, agent_version, verdict, legal_basis, reason_de, confidence, trace_id)
values (%s, %s, %s, %s, %s, %s, %s)
on conflict (content_hash, agent_version) do nothing
"""

SELECT = """
select content_hash, agent_version, verdict, legal_basis, reason_de, confidence, trace_id
from verdicts where content_hash = %s and agent_version = %s
"""

_COLUMNS = (
    "content_hash",
    "agent_version",
    "verdict",
    "legal_basis",
    "reason_de",
    "confidence",
    "trace_id",
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


def ensure_table() -> None:
    """Fail early, from the caller's setup path, if the migrations have not been applied.

    Deliberately not called by `save`, and deliberately not creating anything:
    the schema is the store's migrations' job (`python -m app.store.migrate`).
    """
    with connection() as conn:
        row = conn.execute(
            "select 1 from information_schema.columns"
            " where table_schema = current_schema()"
            " and table_name = 'verdicts' and column_name = 'content_hash'"
        ).fetchone()
    if not row:
        raise RuntimeError("verdicts table is missing or old; run `python -m app.store.migrate`")


def save(record: VerdictRecord, version: str, content_hash: str) -> None:
    with connection() as conn:
        conn.execute(
            INSERT,
            (
                content_hash,
                version,
                record.verdict,
                record.legal_basis,
                record.reason_de,
                record.confidence,
                record.langfuse_trace_id,
            ),
        )


def get(content_hash: str, version: str) -> ClauseVerdict | None:
    """The cached verdict for this clause text under this agent version, if any."""
    with connection() as conn:
        row = conn.execute(SELECT, (content_hash, version)).fetchone()
    if not row:
        return None
    return ClauseVerdict(**dict(zip(_COLUMNS, row, strict=True)))

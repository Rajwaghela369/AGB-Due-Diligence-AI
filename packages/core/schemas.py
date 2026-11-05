"""The data contracts shared across the component boundary.

Pipelines write these, the app and evals read them. Phase 1 and Phase 2 fill in
the remaining fields; the records below are the shape agreed in the design docs.
"""

from typing import Literal

from pydantic import BaseModel, Field

# "general" chunks join every candidate set regardless of the router's choice;
# "other" chunks are indexed and searchable but never forced in.
Topic = Literal["liability", "warranty", "general", "other"]
Verdict = Literal["valid", "potentially_void", "uncertain"]


class LawChunk(BaseModel):
    """One retrievable unit of the BGB. Produced by P1, read by the agent."""

    chunk_id: str  # deterministic: law-paragraph-item-subitem, e.g. bgb-309-7
    law: str
    paragraph: str
    item: str | None = None
    sub_item: str | None = None
    title: str
    topics: list[Topic]
    embed_text: str  # law/§/title/stem prefix, used only for embedding
    text: str  # clean text for display and citation
    source_dump: str
    source_url: str | None = None


class Clause(BaseModel):
    """One AGB clause. Produced by P2. Carries NO label."""

    clause_id: str
    contract_id: int
    title: str
    text: str
    topics: list[Topic]


class LabeledClause(Clause):
    """A clause with its ground truth. Lives only in the Langfuse dataset."""

    label_void: bool


class VerdictRecord(BaseModel):
    """One agent decision for one clause in one run."""

    run_id: str
    clause_id: str
    verdict: Verdict
    legal_basis: list[str] = Field(default_factory=list)  # chunk_ids
    reason_de: str
    confidence: float
    lawyer_decision: Verdict | None = None
    langfuse_trace_id: str | None = None


class StoreManifest(BaseModel):
    """Written next to the vector store by P1, asserted by the app at startup."""

    collection: str
    embedding_model: str
    embedding_dimension: int
    source_dump: str
    chunk_count: int
    config_hash: str
    built_at: str

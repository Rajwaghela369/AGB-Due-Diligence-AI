"""The one retrieval path, shared by the agent and the retrieval scorer.

It lives in core because `app` may not import `pipelines`: without this module
the agent would carry its own copy of the search and the 0.258 re-check would
measure a lookalike. The scorer embeds a whole gold file in one Jina call and
then searches per query; the agent uses `retrieve` for one clause at a time.
Both go through `search`, so the policy and the filter are identical.
"""

from dataclasses import dataclass
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

from core import qdrant as store
from core.config import config_hash, index_config_hash, load_yaml
from core.embeddings import TASK_QUERY, EmbeddingBatch, embed_texts
from core.hashing import content_hash
from core.paths import CONFIGS


@dataclass(frozen=True)
class Retrieved:
    chunk_id: str
    citation: str
    text: str
    score: float
    rank: int


# One assertion per collection per process: the manifest does not change
# underneath a running job, and re-reading it on every clause costs a round trip.
_validated: dict[str, str] = {}


def _kb() -> dict[str, Any]:
    return load_yaml(CONFIGS / "kb.yaml")


def _expected_index_hash(collection: str, kb: dict[str, Any]) -> str:
    """Return the lineage hash for the index selected by `collection`."""
    bgb = kb["store"]
    if collection in {bgb["alias"], bgb["collection"]}:
        bgb_kb = {key: value for key, value in kb.items() if key != "dsgvo_avv"}
        return index_config_hash(bgb_kb, load_yaml(CONFIGS / "kb_paragraphs.yaml"))

    avv = kb.get("dsgvo_avv")
    if avv is not None and collection in {avv["store"]["alias"], avv["store"]["collection"]}:
        index_config = {
            "source": avv["source"],
            "embedding": kb["embedding"],
            "store": avv["store"],
            "chunking": avv["chunking"],
        }
        return config_hash(index_config)

    return index_config_hash(kb, load_yaml(CONFIGS / "kb_paragraphs.yaml"))


def paragraph_key(value: str | int) -> str:
    """The `paragraph` payload value for a section reference.

    The P1 chunker stores paragraphs as "§ 305", "§ 309" - the citation form,
    not the bare number - so a policy written as `[305, 305a]` has to be mapped
    before it can match anything. Sending "305" matches no chunk and raises
    nothing, which would silently return no candidates.
    """
    text = str(value).strip()
    if text.startswith("§"):
        text = text[1:].strip()
    return f"§ {text}"


def build_filter(topic: str, policy: dict[str, Any]) -> Filter:
    """Narrow by topic without ever gating evidence away.

    The router's topic is a hint, so `general` chunks stay in the candidate set
    whatever it picks, and the AGB-control paragraphs can be forced in whatever
    they are tagged. A mis-routed clause should cost ranking, not evidence.
    """
    # An empty topic means "do not narrow". Keeping it would emit a condition
    # matching the literal empty string, which no chunk carries - and because
    # Qdrant requires at least one `should` to match, every query would return
    # nothing. The v1 agent passes topic="" deliberately.
    topics = {t for t in {topic, *policy.get("always_include_topics", [])} if t}
    paragraphs = {paragraph_key(p) for p in policy.get("always_include_paragraphs", [])}

    should = [
        FieldCondition(key="topics", match=MatchValue(value=value)) for value in sorted(topics)
    ]
    should.extend(
        FieldCondition(key="paragraph", match=MatchValue(value=value))
        for value in sorted(paragraphs)
    )
    must = [FieldCondition(key="kind", match=MatchValue(value=store.KIND_CHUNK))]

    # No topic and no forced paragraphs: search every chunk rather than none.
    # A topic is only a ranking hint; broad fallback keeps mis-tagged legal norms
    # reachable.
    if not should:
        return Filter(must=must)
    return Filter(must=must, should=should)


def _assert_manifest(qdrant: Any, collection: str, kb: dict[str, Any]) -> None:
    """Refuse an index built from a different configuration than the one in the repo."""
    expected = _expected_index_hash(collection, kb)
    if _validated.get(collection) == expected:
        return

    manifest = store.read_manifest(qdrant, collection)
    if manifest is None:
        raise RuntimeError(f"Qdrant collection {collection!r} has no store manifest")
    if manifest.config_hash != expected:
        raise RuntimeError(
            f"Qdrant collection {collection!r} was built with config {manifest.config_hash}, "
            f"but the repo's kb.yaml hashes to {expected}; rebuild the knowledge base, or if "
            "only lineage-excluded keys moved, run scripts/migrate_manifest.py"
        )
    if manifest.embedding_model != kb["embedding"]["model"]:
        raise RuntimeError(
            f"manifest embedding model {manifest.embedding_model!r} does not match "
            f"kb.yaml {kb['embedding']['model']!r}"
        )
    _validated[collection] = expected


def embed_queries(
    texts: list[str], kb: dict[str, Any] | None = None, cache: object | None = None
) -> EmbeddingBatch:
    """Embed search queries in one API call, with the query-side adapter."""
    embedding = (kb or _kb())["embedding"]
    return embed_texts(
        texts,
        model=embedding["model"],
        dimension=embedding["dimension"],
        task=embedding.get("task_query", TASK_QUERY),
        batch_size=embedding["batch_size"],
        normalized=embedding["normalize"],
        cache=cache,
    )


def search(
    vector: list[float],
    topic: str,
    policy: dict[str, Any],
    depth: int = 20,
    collection: str | None = None,
    qdrant: Any = None,
) -> list[Retrieved]:
    """Ranked chunks for one query vector.

    `collection=None` reads the alias, which is what the app and the P2 re-check
    want. The P1 ingest gate passes the versioned collection it just built,
    because it runs before the alias moves.
    """
    kb = _kb()
    target = collection or kb["store"]["alias"]
    qdrant = qdrant or store.client()
    _assert_manifest(qdrant, target, kb)

    hits = qdrant.query_points(
        target,
        query=vector,
        limit=max(1, depth),
        query_filter=build_filter(topic, policy),
        with_payload=True,
    ).points

    results = []
    for rank, hit in enumerate(hits, start=1):
        payload = hit.payload or {}
        results.append(
            Retrieved(
                chunk_id=str(payload["chunk_id"]),
                citation=str(payload.get("citation", payload["chunk_id"])),
                text=str(payload.get("text", "")),
                score=float(hit.score),
                rank=rank,
            )
        )
    return results


def retrieve(
    text: str,
    topic: str,
    policy: dict[str, Any],
    depth: int = 20,
    collection: str | None = None,
    cache: object | None = None,
) -> list[Retrieved]:
    """One clause in, ranked chunks out, reusing the policy-specific result."""
    kb = _kb()
    current_index_hash = index_config_hash(kb, load_yaml(CONFIGS / "kb_paragraphs.yaml"))
    # Topic and depth change the candidate set as much as the policy does.
    policy_hash = config_hash({"policy": policy, "topic": topic, "depth": depth})
    digest = content_hash(text)
    if cache is not None:
        stored = cache.get_retrieval(digest, current_index_hash, policy_hash)
        if stored is not None:
            return [item if isinstance(item, Retrieved) else Retrieved(**item) for item in stored]

    vector = embed_queries([text], kb, cache=cache).vectors[0]
    results = search(vector, topic, policy, depth=depth, collection=collection)
    if cache is not None:
        cache.put_retrieval(
            digest,
            current_index_hash,
            policy_hash,
            [
                {
                    "chunk_id": item.chunk_id,
                    "citation": item.citation,
                    "text": item.text,
                    "score": item.score,
                    "rank": item.rank,
                }
                for item in results
            ],
        )
    return results

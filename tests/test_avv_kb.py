"""The AVV statute set is reproducible without embedding or store access."""

import json
from pathlib import Path

import pytest
from core.config import load_yaml
from core.paths import CONFIGS, PROCESSED, RAW
from core.schemas import LawChunk

from pipelines.p1_kb.dsgvo_avv import RAW_FILENAME, build_live_index, chunk_dsgvo


def _chunks(path: Path = PROCESSED / "dsgvo_chunks.jsonl") -> list[LawChunk]:
    return [LawChunk(**json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


def test_official_dsgvo_chunks_are_reproducible_offline(tmp_path):
    manifest = json.loads((RAW / "dsgvo_avv" / "dsgvo_source.json").read_text(encoding="utf-8"))
    output = tmp_path / "chunks.jsonl"

    produced = chunk_dsgvo(
        RAW / "dsgvo_avv" / RAW_FILENAME,
        output,
        retrieved_at=manifest["retrieved_at"],
    )

    assert [chunk.model_dump() for chunk in produced] == [chunk.model_dump() for chunk in _chunks()]
    assert len(produced) == 32


def test_chunk_set_covers_definitions_art_28_and_art_32_at_item_granularity():
    chunks = _chunks()
    citations = {chunk.citation for chunk in chunks}

    assert {f"Art. 4 Nr. {number} DSGVO" for number in (1, 2, 7, 8, 26)} <= citations
    assert {f"Art. 28 Abs. 3 lit. {letter} DSGVO" for letter in "abcdefgh"} <= citations
    assert {f"Art. 32 Abs. 1 lit. {letter} DSGVO" for letter in "abcd"} <= citations
    assert {chunk.paragraph for chunk in chunks} == {"Art. 4", "Art. 28", "Art. 32"}
    assert len({chunk.chunk_id for chunk in chunks}) == len(chunks)


def test_lit_c_uses_the_official_cross_reference_and_citation():
    lit_c = next(chunk for chunk in _chunks() if chunk.chunk_id == "dsgvo-28-abs3-lit-c")

    assert lit_c.citation == "Art. 28 Abs. 3 lit. c DSGVO"
    assert lit_c.text == "alle gemäß Artikel 32 erforderlichen Maßnahmen ergreift"
    assert lit_c.source_url and "32016R0679" in lit_c.source_url


def test_dsgvo_collection_is_separate_and_live_build_is_guarded():
    config = load_yaml(CONFIGS / "kb.yaml")["dsgvo_avv"]

    assert config["store"] == {
        "backend": "qdrant",
        "collection": "dsgvo_avv_v1",
        "alias": "dsgvo_avv",
        "distance": "Cosine",
        "id_strategy": "uuid5_of_chunk_id",
        "payload_indexes": ["paragraph", "kind"],
    }
    with pytest.raises(PermissionError, match="approve-live-index"):
        build_live_index(PROCESSED / "dsgvo_chunks.jsonl", {"dsgvo_avv": config}, approved=False)


def test_statute_source_records_official_url_hash_and_reuse_terms():
    source = json.loads((RAW / "dsgvo_avv" / "dsgvo_source.json").read_text(encoding="utf-8"))

    assert source["celex"] == "32016R0679"
    assert len(source["sha256"]) == 64
    assert "Decision 2011/833/EU" in source["licence"]
    assert source["licence_url"].startswith("https://commission.europa.eu/")

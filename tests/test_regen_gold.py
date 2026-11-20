import pandas as pd
import pytest

from pipelines.p2_eval.s6_regen_gold import GoldMapError, build_gold, router_topic, write_gold

CHUNKS = {"bgb-309-7-a", "bgb-309-7-b", "bgb-439-abs1", "bgb-476-abs1"}


def _dev(rows):
    return pd.DataFrame(rows)


def _clause(
    clause_id="agb-349", topics=("liability",), void=True, text="Wir haften nur bei Vorsatz."
):
    return {
        "clause_id": clause_id,
        "contract_id": 12,
        "topics": list(topics),
        "text": text,
        "label_void": void,
    }


def _entry(clause_id="agb-349", primary="bgb-309-7-a", **extra):
    return {"clause_id": clause_id, "primary_chunk_id": primary, **extra}


@pytest.mark.parametrize(
    ("topics", "override", "expected"),
    [
        (["liability"], None, "liability"),
        (["other", "warranty"], None, "warranty"),
        # Eight in-scope clauses carry both; the mapping decides.
        (["liability", "warranty"], "warranty", "warranty"),
        (["liability", "warranty"], None, "liability"),
    ],
)
def test_router_topic(topics, override, expected):
    assert router_topic(topics, override) == expected


def test_build_gold_takes_the_text_from_the_corpus_not_the_mapping():
    dev = _dev([_clause(text="Die Haftung ist ausgeschlossen.")])

    rows = build_gold([_entry()], dev, CHUNKS)

    assert len(rows) == 1
    assert rows[0]["query"] == "Die Haftung ist ausgeschlossen."
    assert rows[0]["query_id"] == "agb-349"
    assert rows[0]["topic"] == "liability"
    assert rows[0]["confidence"] == "normal"


def test_build_gold_carries_the_low_confidence_flag():
    dev = _dev([_clause()])

    rows = build_gold([_entry(confidence="low", note="arguable")], dev, CHUNKS)

    assert rows[0]["confidence"] == "low"


def test_build_gold_rejects_a_clause_that_is_not_void():
    dev = _dev([_clause(void=False)])

    with pytest.raises(GoldMapError, match="not labelled void"):
        build_gold([_entry()], dev, CHUNKS)


def test_build_gold_rejects_a_clause_outside_the_dev_half():
    dev = _dev([_clause(clause_id="agb-349")])

    with pytest.raises(GoldMapError, match="not in the dev half"):
        build_gold([_entry(clause_id="agb-9999")], dev, CHUNKS)


def test_build_gold_rejects_a_chunk_id_absent_from_the_index():
    dev = _dev([_clause()])

    with pytest.raises(GoldMapError, match="bgb-999"):
        build_gold([_entry(primary="bgb-999")], dev, CHUNKS)


def test_build_gold_rejects_also_relevant_pointing_at_a_missing_chunk():
    dev = _dev([_clause()])

    with pytest.raises(GoldMapError, match="bgb-nope"):
        build_gold([_entry(also_relevant=["bgb-nope"])], dev, CHUNKS)


def test_build_gold_refuses_a_partial_key():
    # A void clause left unmapped would quietly shrink the answer key.
    dev = _dev([_clause(clause_id="agb-349"), _clause(clause_id="agb-456")])

    with pytest.raises(GoldMapError, match="agb-456"):
        build_gold([_entry(clause_id="agb-349")], dev, CHUNKS)


def test_write_gold_matches_the_phase_1_schema(tmp_path):
    import json

    dev = _dev([_clause()])
    rows = build_gold([_entry(also_relevant=["bgb-309-7-b"])], dev, CHUNKS)

    path = write_gold(rows, tmp_path)
    written = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    # The P1 scorer reads these four keys; it must work unchanged.
    assert path.name == "agb_queries.jsonl"
    assert {"query_id", "topic", "primary_chunk_id", "also_relevant"} <= set(written[0])
    assert written[0]["also_relevant"] == ["bgb-309-7-b"]

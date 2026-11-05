import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from pipelines.p1_kb.s2_select_norms import UnresolvedParagraphs, select_norms

COLUMNS = ["section", "title", "content", "markdown_content", "doknr", "updated_date"]


def _snapshot(tmp_path, sections):
    rows = [
        {
            "section": s,
            "title": f"Titel {s}",
            "content": f"<P>(1) Text zu {s}.</P>",
            "markdown_content": f"(1) Text zu {s}.",
            "doknr": "X",
            "updated_date": "2026-05-08T22:02:26Z",
        }
        for s in sections
    ]
    path = tmp_path / "snap.parquet"
    schema = pa.schema([(c, pa.string()) for c in COLUMNS])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)
    return path


def test_resolves_and_carries_chunking_instructions(tmp_path):
    snapshot = _snapshot(tmp_path, ["§ 307", "§ 309", "§ 999"])
    whitelist = [
        {"ref": "§ 307", "level": "absatz", "topics": ["general"]},
        {"ref": "§ 309", "level": "item", "topics": ["other"], "items": {"7": ["liability"]}},
    ]

    result = select_norms(snapshot, whitelist, {"revision": "abc"}, tmp_path)

    assert (result.selected, result.item_level, result.absatz_level) == (2, 1, 1)
    records = [json.loads(line) for line in result.path.read_text(encoding="utf-8").splitlines()]
    assert [r["section"] for r in records] == ["§ 307", "§ 309"]
    assert records[1]["item_topics"] == {"7": ["liability"]}
    assert records[0]["source"]["revision"] == "abc"


def test_missing_section_fails_the_run(tmp_path):
    snapshot = _snapshot(tmp_path, ["§ 307"])
    whitelist = [
        {"ref": "§ 307", "level": "absatz", "topics": ["general"]},
        {"ref": "§ 309", "level": "item", "topics": ["other"]},
    ]

    with pytest.raises(UnresolvedParagraphs, match="§ 309"):
        select_norms(snapshot, whitelist, {}, tmp_path)

import pandas as pd
import pytest

from pipelines.p2_eval.s2_normalize import (
    extract_citations,
    normalize,
    split_title,
    to_agent_topics,
)

PLACEHOLDERS = ("<<NAME>>", "<<STADT>>", "<<STRASSE>>")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("10. Gewährleistung", ("10", "Gewährleistung")),
        ("§8 Mängelgewährleistung, Garantie", ("8", "Mängelgewährleistung, Garantie")),
        ("§4 - LIEFERUNG UND VERSANDKOSTEN", ("4", "LIEFERUNG UND VERSANDKOSTEN")),
        ("5.      Eigentums- und Gefahrenübergang", ("5", "Eigentums- und Gefahrenübergang")),
        ("5. Nutzungsbedingungen ", ("5", "Nutzungsbedingungen")),
        # Number with no heading, and a heading with no number: both occur.
        ("5", ("5", None)),
        ("7.4.: ", ("7.4", None)),
        ("Gewährleistung", (None, "Gewährleistung")),
        # 12 rows in the corpus have no title at all.
        (None, (None, None)),
        ("", (None, None)),
    ],
)
def test_split_title(title, expected):
    assert split_title(title) == expected


def test_extract_citations_records_the_law_only_when_stated():
    text = "Es gilt § 478 BGB. Die Rügepflicht nach § 377 HGB bleibt. Siehe auch § 10."

    citations = extract_citations(text)

    assert [(c["paragraph"], c["law"]) for c in citations] == [
        ("478", "BGB"),
        ("377", "HGB"),
        # A bare number is the shop's own section far more often than a
        # statute, so the law is left unknown rather than assumed to be BGB.
        ("10", None),
    ]


def test_extract_citations_keeps_modifiers():
    assert extract_citations("Vgl. § 281 Abs. 1 BGB")[0]["raw"] == "§ 281 Abs. 1"


def test_extract_citations_empty_when_none_cited():
    assert extract_citations("Wir haften nur bei Vorsatz.") == []


def test_to_agent_topics_collapses_everything_outside_the_remit():
    assert to_agent_topics(["warranty", "delivery"]) == ["other", "warranty"]
    assert to_agent_topics(["liability", "warranty"]) == ["liability", "warranty"]
    assert to_agent_topics(["payment"]) == ["other"]
    assert to_agent_topics([]) == []


def _corpus(tmp_path, rows):
    path = tmp_path / "agb.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def _row(**overrides):
    row = {
        "id": 1,
        "contract": 1,
        "lang": "de",
        "title": "6. Haftung",
        "text": "Wir haften unbeschränkt für Vorsatz und grobe Fahrlässigkeit.",
        "topics": "liability",
        "subtopics": None,
        "void": 0.0,
    }
    return row | overrides


def test_normalize_types_the_label_and_flags_fragments(tmp_path):
    corpus = _corpus(
        tmp_path,
        [
            _row(id=1, void=1.0),
            _row(id=2, contract=29, title="12.3", text="12.3 WANN SIE ANWENDBAR SIND"),
        ],
    )

    result = normalize(corpus, fragment_chars=40, out_dir=tmp_path)
    frame = pd.read_parquet(result.path)

    assert (result.rows, result.fragments) == (2, 1)
    assert list(frame.clause_id) == ["agb-1", "agb-2"]
    assert list(frame.label_void) == [True, False]
    assert list(frame.is_fragment) == [False, True]


def test_normalize_leaves_anonymisation_placeholders_untouched(tmp_path):
    text = "Betreiber ist <<NAME>>, <<STRASSE>>, <<STADT>>. Die Haftung ist beschränkt."
    corpus = _corpus(tmp_path, [_row(text=text)])

    result = normalize(corpus, fragment_chars=40, out_dir=tmp_path)

    stored = pd.read_parquet(result.path).text.iloc[0]
    assert stored == text
    assert all(placeholder in stored for placeholder in PLACEHOLDERS)


def test_normalize_keeps_the_raw_topic_vocabulary_alongside_the_mapped_one(tmp_path):
    corpus = _corpus(tmp_path, [_row(topics="delivery,warranty", subtopics="warranty:period")])

    result = normalize(corpus, fragment_chars=40, out_dir=tmp_path)
    frame = pd.read_parquet(result.path)

    assert list(frame.topics_raw.iloc[0]) == ["delivery", "warranty"]
    assert list(frame.topics.iloc[0]) == ["other", "warranty"]
    assert list(frame.subtopics.iloc[0]) == ["warranty:period"]

"""Synthetic layouts pin the splitter's numbering and page-cleanup behavior."""

from __future__ import annotations

from core.hashing import content_hash

from app.ingest.split import split, words_from_text


def _words(lines):
    words = []
    for page, line, text, top in lines:
        x = 50.0
        for token in text.split():
            width = len(token) * 5.0
            words.append(
                {
                    "text": token,
                    "page": page,
                    "line": line,
                    "x0": x,
                    "top": top,
                    "x1": x + width,
                    "bottom": top + 10,
                    "page_height": 800,
                }
            )
            x += width + 4
    return words


def test_every_supported_numbering_style_and_heading_context():
    clauses = split(
        _words(
            [
                (1, 0, "§ 6 Haftung", 100),
                (1, 1, "(1) Wir haften unbeschränkt.", 120),
                (1, 2, "a) Dies gilt bei Vorsatz.", 140),
                (1, 3, "1) Schäden sind nachzuweisen.", 160),
                (1, 4, "6. Eine weitere Regel gilt.", 180),
                (1, 5, "6.1 Diese Regel ist genauer.", 200),
                (1, 6, "6.1.2 Dies ist die kleinste Einheit.", 220),
            ]
        )
    )

    assert [clause.number for clause in clauses] == ["(1)", "a)", "1)", "6", "6.1", "6.1.2"]
    assert all(clause.heading == "§ 6 Haftung" for clause in clauses)
    assert clauses[0].rects[0].page == 1
    assert clauses[0].content_hash == content_hash(clauses[0].text)
    assert all(clause.in_scope for clause in clauses)


def test_heading_only_layout_sets_nearest_heading():
    clauses = split(
        _words(
            [
                (1, 0, "Haftung", 100),
                (1, 1, "Die Haftung ist begrenzt.", 120),
                (1, 2, "Gewährleistung", 160),
                (1, 3, "Mängel sind unverzüglich anzuzeigen.", 180),
            ]
        )
    )

    assert [(clause.heading, clause.text) for clause in clauses] == [
        ("Haftung", "Die Haftung ist begrenzt."),
        ("Gewährleistung", "Mängel sind unverzüglich anzuzeigen."),
    ]


def test_cross_page_join_dehyphenates_and_drops_running_matter():
    clauses = split(
        _words(
            [
                (1, 0, "Muster GmbH AGB", 20),
                (1, 1, "1. Die Gewährleis-", 700),
                (1, 2, "1", 780),
                (2, 0, "Muster GmbH AGB", 20),
                (2, 1, "tung beträgt zwei Jahre.", 80),
                (2, 2, "2", 780),
            ]
        )
    )

    assert len(clauses) == 1
    assert clauses[0].text == "1. Die Gewährleistung beträgt zwei Jahre."
    assert [rect.page for rect in clauses[0].rects] == [1, 2]


def test_pasted_text_uses_same_splitter_with_offsets_and_no_rects():
    text = "Haftung\n(1) Wir haften bei Vorsatz.\n(2) Im Übrigen nicht."
    clauses = split(words_from_text(text))

    assert [clause.number for clause in clauses] == ["(1)", "(2)"]
    assert all(clause.heading == "Haftung" for clause in clauses)
    assert all(not clause.rects and clause.page is None for clause in clauses)
    assert text[clauses[0].start : clauses[0].end] == "(1) Wir haften bei Vorsatz."


def test_marker_on_its_own_line_still_numbers_the_following_prose():
    clauses = split(
        _words(
            [
                (1, 0, "§ 6 Haftung", 100),
                (1, 1, "(1)", 120),
                (1, 2, "Wir haften bei Vorsatz.", 140),
                (1, 3, "a)", 160),
                (1, 4, "Im Übrigen nicht.", 180),
            ]
        )
    )

    assert [(clause.number, clause.text) for clause in clauses] == [
        ("(1)", "(1) Wir haften bei Vorsatz."),
        ("a)", "a) Im Übrigen nicht."),
    ]

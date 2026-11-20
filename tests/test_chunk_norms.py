from pipelines.p1_kb.s3_chunk_norms import chunk_record

SOURCE = {"config": "dump-test"}


def _record(content, level="absatz", **extra):
    return {
        "section": "§ 309",
        "title": "Klauselverbote",
        "content": content,
        "level": level,
        "topics": ["other"],
        "source": SOURCE,
        **extra,
    }


def test_absatz_split_keeps_stem_list_and_tail():
    content = (
        "<P>(1) Erster Absatz.</P>"
        "<P>(2) Die Sache entspricht, wenn sie <DL><DT>1.</DT><DD><LA>passt,</LA></DD>"
        "<DT>2.</DT><DD><LA>taugt.</LA></DD></DL>Nachgestellter Satz.</P>"
    )
    chunks = chunk_record(_record(content))

    assert [c.chunk_id for c in chunks] == ["bgb-309-abs1", "bgb-309-abs2"]
    second = chunks[1].text
    assert second.startswith("Die Sache entspricht")
    assert "1. passt," in second and "2. taugt." in second
    assert second.endswith("Nachgestellter Satz.")


def test_unnumbered_paragraph_continues_the_previous_absatz():
    content = "<P>(1) Erster Satz.</P><P>Fortsetzung ohne Nummer.</P>"
    chunks = chunk_record(_record(content))

    assert len(chunks) == 1
    assert chunks[0].text == "Erster Satz. Fortsetzung ohne Nummer."


def test_single_unnumbered_paragraph_is_the_whole_norm():
    chunks = chunk_record(_record("<P>Nur ein Satz.</P>"))

    assert chunks[0].chunk_id == "bgb-309"
    assert chunks[0].citation == "§ 309"


def test_items_carry_the_stem_and_drop_the_heading_into_the_title():
    content = (
        "<P>In AGB ist unwirksam <DL>"
        "<DT>1.</DT><DD><LA>(Preiserhöhung)eine Bestimmung dazu;</LA></DD>"
        "</DL></P>"
    )
    chunks = chunk_record(_record(content, level="item"))

    assert chunks[0].chunk_id == "bgb-309-1"
    assert chunks[0].citation == "§ 309 Nr. 1"
    assert chunks[0].title == "Preiserhöhung"
    assert chunks[0].text == "In AGB ist unwirksam eine Bestimmung dazu;"


def test_split_subitem_places_the_closing_qualifier_after_the_subitem():
    # Mirrors § 309 Nr. 7: the lead-in and the qualifier sit in separate <LA>
    # siblings around the nested list.
    content = (
        "<P>In AGB ist unwirksam <DL>"
        "<DT>7.</DT><DD>"
        "<LA>(Haftung)<DL><DT>a)</DT><DD><LA>(Leben)Ausschluss fuer Leben;</LA></DD>"
        "<DT>b)</DT><DD><LA>(Verschulden)Ausschluss bei grober Fahrlaessigkeit;</LA></DD></DL></LA>"
        "<LA>die Buchstaben a und b gelten nicht fuer Sonderfaelle;</LA>"
        "</DD></DL></P>"
    )
    chunks = chunk_record(_record(content, level="item", split_subitems=["7"]))

    assert [c.chunk_id for c in chunks] == ["bgb-309-7-a", "bgb-309-7-b"]
    assert chunks[0].title == "Haftung — Leben"
    assert chunks[0].citation == "§ 309 Nr. 7 lit. a"
    assert chunks[0].text == (
        "In AGB ist unwirksam Ausschluss fuer Leben; "
        "die Buchstaben a und b gelten nicht fuer Sonderfaelle;"
    )


def test_unsplit_item_keeps_its_subitems_inline():
    content = (
        "<P>In AGB ist unwirksam <DL>"
        "<DT>12.</DT><DD><LA>(Beweislast)eine Bestimmung, indem er"
        "<DL><DT>a)</DT><DD><LA>erstes;</LA></DD><DT>b)</DT><DD><LA>zweites;</LA></DD></DL></LA></DD>"
        "</DL></P>"
    )
    chunks = chunk_record(_record(content, level="item"))

    assert [c.chunk_id for c in chunks] == ["bgb-309-12"]
    assert "a) erstes;" in chunks[0].text
    assert "b) zweites;" in chunks[0].text


def test_item_topics_override_the_paragraph_topics():
    content = (
        "<P>In AGB ist unwirksam <DL>"
        "<DT>1.</DT><DD><LA>erstes;</LA></DD>"
        "<DT>7.</DT><DD><LA>siebtes;</LA></DD></DL></P>"
    )
    chunks = chunk_record(_record(content, level="item", item_topics={"7": ["liability"]}))

    assert chunks[0].topics == ["other"]
    assert chunks[1].topics == ["liability"]

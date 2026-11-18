"""Regression tests from the 9.A security review (reports/p9-masking-review.md).

Each block is one of the reviewer's probes (`maskprobe/probe1.py` … `probe8.py`), turned
into assertions. Regex-only, as the probes ran: the strictest case, since NER would only
add detections. All offline.
"""

from __future__ import annotations

import json
import time

import pytest
from core import masking
from core.masking import MaskedText, MaskingBoundary, PresidioMasker, boundary


@pytest.fixture(autouse=True)
def _masking_off():
    masking.configure(None)
    yield
    masking.configure(None)


def fresh(text: str) -> MaskedText:
    return PresidioMasker(allow_regex_only=True).mask(text)


# --- probe 1 and 4 (finding 4, 6): detection misses -------------------------------------

LEAKS = [
    ("Rufen Sie uns an: 030 / 123 456 78.", ["123 456 78"]),
    ("Erreichbar unter 0171 1234567 werktags.", ["1234567"]),
    ("Hotline 01711234567", ["01711234567"]),
    ("+49 (0)30 12345678", ["12345678"]),
    ("Bitte melden unter 03012345678 oder schriftlich.", ["03012345678"]),
    ("Überweisung an DE89 3704 0044 0532 0130 00 bitte.", ["3704 0044"]),
    ("iban: de89370400440532013000", ["de8937"]),
    ("Konto DE12 3456 7890 1234 5678 90", ["3456 7890"]),
    ("Kontakt: Max Mustermann <max.mustermann@example.de>", ["Mustermann"]),
    ("anna(at)beispiel.de oder anna [at] beispiel [dot] de", ["anna", "beispiel"]),
    ("Amtsgericht München, HRB 123456", ["123456"]),
    ("Registergericht Charlottenburg HRB 98765 B", ["98765"]),
    ("Handelsregister: Nr. 12345, Amtsgericht Köln", ["12345"]),
    ("Lieferadresse: Hauptstraße 5, 10115 Berlin", ["Hauptstraße 5", "10115"]),
    ("Kaiser-Wilhelm-Straße 12a", ["Kaiser-Wilhelm-Straße"]),
    ("Bahnhofstr. 3, 80335 München", ["Bahnhofstr. 3", "80335"]),
    ("Unter den Linden 77, 10117 Berlin", ["Linden 77", "10117"]),
    ("Frau Jördis Ülkü-Özdemir erklärt", ["Jördis", "Özdemir"]),
    ("Die Ware holt Björn Gößling ab.", ["Gößling"]),
    ("Ansprechpartner ist herr müller", ["müller"]),
    ("Geschäftsführer: Dr. Hans-Peter Großmann, Jörg Ähnlich", ["Großmann", "Ähnlich"]),
    ("Kontoinhaber: Lieselotte Brennecke", ["Brennecke"]),
    ("Steuer-ID 12 345 678 901", ["678 901"]),
    ("geboren am 3. März 1985", ["1985"]),
    ("Geburtsdatum: 1985-03-03", ["1985-03-03"]),
    ("Profil: https://www.example.de/mitarbeiter/anna-mueller", ["anna-mueller"]),
    ("mailto:anna@example.de", ["anna@example.de"]),
    ("Sitz der Gesellschaft: 80331 München", ["80331"]),
    ("München, den 1.1.2024\nLieselotte Brennecke", ["Brennecke"]),
    ("Lieselotte Brenneckes Haftung", ["Brennecke"]),
    ("Kontoinhaber: Lieselotte Brennecke. LIESELOTTE BRENNECKE haftet.", ["BRENNECKE"]),
    ("Ansprechpartnerin:\nGundula Quast\nMusterweg 4\n50667 Köln", ["Quast", "50667"]),
    ("Geschäftsführer:\nGundula Quast", ["Quast"]),
    ("Vertreten durch den Geschäftsführer Gundula Quast", ["Quast"]),
    ("Vertreten durch die Geschäftsführerin Gundula Quast", ["Quast"]),
    ("Kontakt: Gundula Quast, Musterweg 4, 50667 Köln", ["Quast", "Musterweg", "50667"]),
    ("Verantwortlich i.S.d. § 18 MStV: Gundula Quast", ["Quast"]),
    ("Inhaber: Gundula Quast e.K.", ["Quast"]),
    ("Datenschutzbeauftragter: Herr RA Gundolf Quast", ["Quast"]),
    ("Gundula Quast\nMusterweg 4\n50667 Köln", ["Quast", "Musterweg", "50667"]),
]


@pytest.mark.parametrize(("text", "values"), LEAKS, ids=[t[:40] for t, _ in LEAKS])
def test_probe_1_and_4_detections(text, values):
    masked = fresh(text)
    assert [value for value in values if value in masked] == []
    assert masked.unmask() == text


def test_probe_4_literal_placeholders_keep_their_meaning():
    masked = fresh("<PERSON_1> schuldet. Frau Quast haftet.")
    assert masked == "<PERSON_1> schuldet. Frau <PERSON_2> haftet."
    assert masked.placeholders == {"<PERSON_2>": "Quast"}


# --- probe 2 (finding 7): false positives that change meaning ---------------------------

UNTOUCHED = [
    "Die Haftung nach § 309 Nr. 7 BGB bleibt unberührt.",
    "Gemäß Art. 6 Abs. 1 lit. b DSGVO verarbeiten wir Ihre Daten.",
    "Es gilt § 476 Abs. 2 BGB sowie § 312g Abs. 2 Nr. 1 BGB.",
    "§ 13 Abs. 1 UWG",
    "Die Max Bahr Baumarkt GmbH haftet.",
    "Paul Hartmann AG, Heidenheim",
    "Hans Wurst Mode KG",
    "Max Planck Institut",
    "Die Lieferung erfolgt durch DHL Paket GmbH.",
    "Sitz der Gesellschaft ist Hamburg.",
    "Lieferfrist 14 Tage, Rücksendung an Lager 12, 12345 Neustadt",
    "Zahlbar innerhalb 030 Tagen",
    "Die Gewährleistung beträgt 24 Monate ab Lieferung (Art. 12345 der Bedingungen).",
    "Rechnungsnummer 2024 / 123456 bitte angeben.",
    "Bestellnummer 0049 123456 angeben",
    "Kosten: 0,30 EUR/Min. aus dem dt. Festnetz, Mobilfunk max. 0,60 EUR/Min.",
    "Die Verjährung beginnt nach § 199 Abs. 1 BGB. Paul haftet nicht.",
    "Inhaber Der Rechte Ist Der Verkäufer",
    "Verantwortliche Stelle Im Sinne Der DSGVO",
    "Vorsitzender Richter Am Landgericht",
    "Notar Kosten Trägt Der Käufer",
    "Unterschrift Des Kunden Erforderlich",
    "Tarifziffer HRB 5 der Preisliste",
    "Siehe PR 2024 und VR 12 Regelungen.",
    "Bitte beachten Sie Ziffer 12 Abs. 3 und Punkt 7 Satz 2.",
    "§§ 305 bis 310 BGB",
    "Art. 246a § 1 Abs. 2 Satz 1 Nr. 1 EGBGB",
    "Das Müller-Verfahren gilt.",
]


@pytest.mark.parametrize("text", UNTOUCHED, ids=[t[:40] for t in UNTOUCHED])
def test_probe_2_legal_prose_and_company_names_are_untouched(text):
    assert fresh(text) == text


# --- probe 3 (finding 1): the summary prompt ---------------------------------------------


def test_probe_3_the_summary_prompt_is_masked_field_by_field():
    from app.review.summary import _masked_prompt

    payload = {
        "findings": [
            {
                "verdict": {"reason_de": "Die Klausel verpflichtet Gundula Quast persönlich."},
                "decision": {
                    "decided_by": "Raj Waghela",
                    "note": "Mit Frau Quast besprochen, Rückruf unter 0221 9876543",
                },
            }
        ],
        "top_risks": [
            {
                "excerpt": "Ansprechpartnerin:\nGundula Quast\nMusterweg 4\n50667 Köln",
                "decided_by": "Raj Waghela",
            }
        ],
    }
    masking.configure(PresidioMasker(allow_regex_only=True))
    with masking.scope("owner", "review", prime=["Ansprechpartnerin: Gundula Quast"]) as active:
        prompt = _masked_prompt(active, payload)
    for value in ("Quast", "Waghela", "Raj", "9876543", "Musterweg", "50667"):
        assert value not in prompt
    sent = json.loads(prompt)
    assert sent["findings"][0]["decision"]["decided_by"] == sent["top_risks"][0]["decided_by"]


# --- probe 5 and 6 (findings 2, 3, 12): no global memory, and the speed -----------------


def test_probe_5_masking_does_not_depend_on_history():
    masker = PresidioMasker(allow_regex_only=True)
    text = "Im Sommer gelten Sonderpreise. Lange Lieferzeiten sind ausgeschlossen."
    masker.mask("Ansprechpartnerin ist Frau Sommer, Vertreter Herr Lange.")
    assert masker.mask(text) == text
    masker.mask("Notar Kosten Trägt Der Käufer")
    other = "Der Käufer trägt die Kosten. Kosten Trägt der Verkäufer."
    assert masker.mask(other) == other


@pytest.mark.parametrize(
    "text",
    [
        "Herr " + "Aaaa " * 20000,
        "+49" + " 1" * 10000 + "x",
        "Tel " + "1 " * 10000,
        "DE89" + " ABCD" * 10000,
        "a" * 10000 + "@" + "b." * 10000,
        "Anna " * 10000,
        "Geschäftsführer: Anna Beispiel. " * 5000,
    ],
    ids=["salutation", "phone", "tel", "iban", "email", "first-names", "repeats"],
)
def test_probe_5_pathological_inputs_stay_linear(text):
    masker = PresidioMasker(allow_regex_only=True)
    started = time.perf_counter()
    masker.mask(text)
    assert time.perf_counter() - started < 2.0


def test_probe_6_a_retrieval_observation_is_masked_fast():
    masker = PresidioMasker(allow_regex_only=True)
    for index in range(3000):  # no memory to fill any more; this is just traffic
        masker.mask(f"Kontoinhaber: Anna Beispiel{index:05d}".replace("0", "a"))
    hook = MaskingBoundary(masker)
    observation = {
        "candidates": [
            {
                "chunk_id": f"bgb-{i}",
                "citation": f"§ {300 + i} BGB",
                "text": "Eine Bestimmung in AGB ist unwirksam, wenn ... " * 5,
                "score": 0.5,
            }
            for i in range(40)
        ]
    }
    started = time.perf_counter()
    for _ in range(10):
        hook.langfuse_mask(data=observation)
    assert (time.perf_counter() - started) / 10 < 0.1  # was 684 ms with a full memory


def test_probe_6_a_trace_is_masked_from_its_scope_not_from_recall():
    """The reviewer's eviction case: a name seen in the prompt, then 2,100 other names,
    then the answer alone. Inside the request's scope it is masked however much traffic
    ran in between; there is no recall to evict."""
    masking.configure(PresidioMasker(allow_regex_only=True))
    with masking.scope("owner", "clause", prime=["Kontoinhaber: Gesine Quarck"]):
        for index in range(2100):
            PresidioMasker(allow_regex_only=True).mask(f"Kontoinhaber: Anna Name{index}")
        traced = boundary().langfuse_mask(data="Gesine Quarck haftet persönlich.")
    assert traced == "<PERSON_1> haftet persönlich."


# --- probe 7 (findings 3, 11): owners and placeholders -----------------------------------


def test_probe_7_another_owners_imprint_changes_nothing():
    masking.configure(PresidioMasker(allow_regex_only=True))
    clause = "Ist der Kunde Kaufmann, juristische Person, so ist Gerichtsstand Hamburg."
    with masking.scope("owner-a", "review-a", prime=["Ansprechpartnerin: Frau Kaufmann"]):
        assert "<PERSON_1>" in boundary().outbound("Frau Kaufmann ruft an.")
    with masking.scope("owner-b", "review-b", prime=[clause]):
        assert boundary().outbound(clause) == clause


def test_probe_7_invented_and_unknown_placeholders_are_shown_neutrally():
    masked = fresh("Frau Quast schuldet <PERSON_1>.")
    assert masked.placeholders == {"<PERSON_2>": "Quast"}
    assert masked.display("<PERSON_2> und <PERSON_1> und <PERSON_7>") == (
        "Quast und [unbekannt] und [unbekannt]"
    )
    masked = fresh("Frau Quast nutzt <NAME_1> und <person_1>")
    assert masked == "Frau <PERSON_1> nutzt <NAME_1> und <person_1>"
    chained = MaskedText("x <PERSON_1> y", {"<PERSON_1>": "<EMAIL_1>"})
    assert chained.unmask("<PERSON_1>") == "<EMAIL_1>"  # one pass, no chain


# --- probe 8 (finding 5): citations do not swallow neighbours -----------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Widerruf gemäß § 5, 0171 1234567 oder per Post.", "1234567"),
        ("Widerruf nach § 355 BGB, 0171 1234567 oder per Post.", "1234567"),
        ("Hinweis nach § 36 VSBG - 030 1234567 Schlichtung", "1234567"),
        ("Gemäß § 3 Abs. 2 bis 0221 9876543 erreichbar", "9876543"),
        ("Vgl. §§ 3, 4, 5 0221-9876543", "9876543"),
        ("Siehe § 2 und Herrn Quast", "Quast"),
    ],
)
def test_probe_8_pii_next_to_a_citation_is_masked(text, value):
    masked = fresh(text)
    assert value not in masked
    assert "§" in masked  # the citation itself stays


def test_probe_8_a_detection_touching_a_citation_keeps_its_outside_part():
    from core.masking import _outside, _protected, _Span

    text = "§ 5, 0171 1234567"
    span = _Span(0, len(text), "PHONE")
    trimmed = _outside(span, _protected(text), text)
    assert trimmed is not None and text[trimmed.start : trimmed.end] == "0171 1234567"


# --- finding 9: Langfuse fields outside the hook -----------------------------------------


def test_a_review_failure_message_is_masked_before_it_becomes_a_status_message():
    from app.review.tracing import ReviewTrace

    class Span:
        def __init__(self):
            self.updates = []

        def update(self, **kwargs):
            self.updates.append(kwargs)

        def end(self):
            return None

    masking.configure(PresidioMasker(allow_regex_only=True))
    span = Span()
    ReviewTrace(span).end(error="ValueError: Rückruf an Frau Quast, 0171 1234567")
    message = span.updates[0]["status_message"]
    assert "Quast" not in message and "1234567" not in message


def test_an_exception_never_leaves_an_observation_open():
    """OpenTelemetry records an exception that leaves a span (message and stack, which no
    mask hook sees); the graph's observations close first and raise afterwards."""
    from app.agent.graph import _observe

    seen = []

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, kind, value, traceback):
            seen.append(kind)
            return False

    class Tracer:
        def start_as_current_observation(self, **kwargs):
            return Context()

    with pytest.raises(ValueError, match="Quast"), _observe(Tracer(), "step"):
        raise ValueError("Frau Quast")
    assert seen == [None]


# --- re-check R1 and R2 ------------------------------------------------------------------


def test_r1_a_cut_excerpt_does_not_leak_a_lone_first_name():
    from app.review.summary import _masked_prompt

    masking.configure(PresidioMasker(allow_regex_only=True))
    clause = "Ansprechpartnerin: Gundula Quast, Musterweg 4, 50667 Köln"
    with masking.scope("owner", "review", prime=[clause]) as active:
        prompt = _masked_prompt(active, {"excerpt": "… Ansprechpartnerin: Gundula"})
    assert "Gundula" not in prompt


def test_r2_an_upper_case_genitive_in_a_trace_is_masked():
    masking.configure(PresidioMasker(allow_regex_only=True))
    with masking.scope("owner", "clause", prime=["Bürgin ist Frau Gesine Quarck."]):
        traced = boundary().langfuse_mask(data="GESINE QUARCKS Bürgschaft")
    assert "QUARCK" not in traced and "GESINE" not in traced

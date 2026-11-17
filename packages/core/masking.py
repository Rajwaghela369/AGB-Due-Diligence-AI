"""The masking boundary: personal data never leaves the server unmasked (Phase 9, 9.A).

Every byte of client text that goes to a model provider (Gemini through `core.llm`, Jina
through `core.embeddings` and `core.routing`) or to Langfuse passes through the one
`MaskingBoundary` here, so there is a single place to get right and a single place to test.
docs/Phase 9 Design.md explains the choices; in short:

- **Where.** Inside the outbound functions themselves, not at their call sites. A call
  site that forgets to mask cannot exist, because no call site masks: `core.llm.decide`
  masks `text`, `core.llm.complete` masks `user`, the Jina functions mask their inputs
  after computing cache keys, and Langfuse gets `langfuse_mask` as its `mask=` hook.
  System prompts are not masked: they are our own versioned templates and never carry
  client text (a contract every prompt builder keeps).
- **Proof.** A masked string is a `MaskedText`, a `str` subclass. Concatenating or slicing
  it gives a plain `str` again, so anything built *after* masking is unmasked by type. A
  strict client (the test fakes) calls `require_masked` on what it receives and refuses
  plain strings while masking is enabled.
- **Reversible, for display only.** `MaskedText.placeholders` maps each placeholder
  (`<PERSON_1>`) back to the original. The map lives only in memory for the duration of
  one call; it is never stored or traced. `unmask` restores a model's answer that quotes
  placeholders before the lawyer sees it.
- **Cache identity stays on the unmasked text.** `core.hashing.content_hash` runs on the
  original clause, so a verdict cached before masking is still found after it.

One module-level boundary, configured once at start-up (`configure`), rather than a masker
threaded through every call: `core.llm` is called from the agent graph, the eval harness and
the review service, and a global boundary is the only way none of them can skip it. Tests
reset it with `configure(None)`.

Detection (`PresidioMasker`) runs in two layers. German regexes find everything with a
fixed shape or a telling context: e-mail, phone, IBAN (checksum), tax and register numbers,
dates of birth, street addresses, URLs with personal paths, and names after a salutation, a
role ("Geschäftsführer:") or a common first name. Presidio with a German spaCy model adds
named-entity recognition for names without such a context. The model is optional: without
it the masker logs a warning once and runs on the regexes alone, so tests and CI never
download it. Statute citations (`§ 309 Nr. 7 BGB`) and existing placeholders are protected
spans that no detector may touch.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from core.logging import get_logger

log = get_logger("core.masking")

# What 9.A masks. Placeholders are `<KIND_n>`, numbered per masked text in order of first
# appearance, so the same name twice in one clause is one placeholder.
EntityKind = Literal[
    "PERSON",
    "ADDRESS",
    "EMAIL",
    "PHONE",
    "IBAN",
    "REGISTER_NUMBER",
    "TAX_ID",
    "DATE_OF_BIRTH",
    "URL",
]
ENTITY_KINDS: tuple[EntityKind, ...] = (
    "PERSON",
    "ADDRESS",
    "EMAIL",
    "PHONE",
    "IBAN",
    "REGISTER_NUMBER",
    "TAX_ID",
    "DATE_OF_BIRTH",
    "URL",
)
PLACEHOLDER = re.compile(r"<(" + "|".join(ENTITY_KINDS) + r")_(\d+)>")


def placeholder(kind: EntityKind, number: int) -> str:
    return f"<{kind}_{number}>"


class MaskedText(str):
    """A string that went through the masker, with the way back.

    Being a `str`, it goes wherever the text went before (a request body, a prompt), so no
    provider call changes shape. Any operation that builds a new string (`+`, `format`,
    slicing) returns a plain `str`: the marker survives only on exactly what was masked.
    """

    placeholders: Mapping[str, str]

    def __new__(cls, text: str, placeholders: Mapping[str, str] | None = None) -> MaskedText:
        masked = super().__new__(cls, text)
        masked.placeholders = dict(placeholders or {})
        return masked

    def unmask(self, text: str | None = None) -> str:
        """`text` (a model's answer quoting placeholders; this text when None) with every
        known placeholder replaced by its original. Unknown placeholders stay as they are."""
        source = str(self) if text is None else text
        if not self.placeholders:
            return source
        return PLACEHOLDER.sub(lambda m: self.placeholders.get(m.group(0), m.group(0)), source)


class Masker(Protocol):
    """Finds personal data in a text and replaces it with placeholders.

    `mask` must be deterministic for a given text and configuration (golden tests, and
    the same prompt for the same clause), must never return the original text for a span
    it detected, and must raise rather than return a half-masked text when it fails.
    """

    def mask(self, text: str) -> MaskedText: ...


class MaskingError(RuntimeError):
    """The masker failed. The text is not sent: the provider call fails like any other
    provider error (a failed result, not a crash)."""


class UnmaskedPayload(RuntimeError):
    """A strict client received text that did not pass the boundary while masking is on."""


class MaskerUnavailable(RuntimeError):
    """The masker cannot load what `privacy.masking.required` demands (Presidio or the
    spaCy model). `build_app` lets this stop the start-up."""


class IdentityMasker:
    """Masks nothing but marks the text as having passed the boundary. For tests of the
    plumbing, and for a deployment that has decided (documented) to send text as is."""

    def mask(self, text: str) -> MaskedText:
        return MaskedText(text)


# --- detection: German patterns ---------------------------------------------------------
#
# Each detector yields `_Span`s; `PresidioMasker.mask` drops the ones that touch a protected
# span or the allow-list, resolves overlaps (longest first) and numbers the rest. The
# patterns lean towards precision on legal prose: a false positive in a clause changes what
# the model judges, so shapes that also occur in statutes (bare five-digit numbers, dates,
# capitalised nouns) are only masked with a context that makes them personal.


@dataclass(frozen=True, order=True)
class _Span:
    start: int
    end: int
    kind: EntityKind


# Latin letters with the diacritics of names common in Germany (Turkish, Polish, Nordic,
# French): "Yıldırım", "Sørensen", "Kowalczyk", "Łukasz".
_UPPER = "A-Z\u00c0-\u00d6\u00d8-\u00deĄĆČĘŁŃŘŚŠŞŻŽĞİ"
_LOWER = "a-z\u00df-\u00f6\u00f8-\u00ffąćčęłńřśšşżžğı"
_WORD_END = rf"(?![{_LOWER}{_UPPER}\w])"

# A name token: "Müller", "Müller-Lüdenscheidt". Not "GmbH" (the boundary forbids "Gmb|H").
_NAME_TOKEN = rf"[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)?{_WORD_END}"
_PARTICLE = r"(?:von|van|de|zu|vom|zur|di|da|ten|del)(?:\s+(?:der|den|dem))?"
_NAME_RUN = re.compile(rf"{_NAME_TOKEN}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_NAME_TOKEN}){{0,3}}")
_TOKEN = re.compile(_NAME_TOKEN)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Capitalised words that end a name run: sentence-initial function words and the nouns
# that follow a name in an imprint ("Anna Müller Telefon 030 …").
_NOT_A_NAME = _words(
    """
    Der Die Das Den Dem Des Ein Eine Einen Einem Einer Eines Sie Er Es Wir Ihr Ihre Ihnen
    Unser Unsere Diese Dieser Dieses Alle Jede Jeder Jedes Kein Keine Bei Mit Von Für Nach
    Und Oder Im In Am An Auf Aus Zum Zur Ist Sind Wird Werden Hat Haben Als Wenn Soweit
    Telefon Tel Fax Telefax Mobil Handy Mail Email Web Internet Website Homepage Adresse
    Anschrift Sitz Registergericht Amtsgericht Handelsregister Registernummer Steuernummer
    Umsatzsteuer Gesellschaft Firma Unternehmen Straße Strasse Postfach Geschäftsführer
    Geschäftsführerin Geschäftsführung Inhaber Inhaberin Vorstand Kontakt Bankverbindung
    Bank Kreditinstitut Sparkasse Volksbank Datenschutz Datenschutzbeauftragter Hinweis
    Vertrag Vertrags Verträge Klausel Klauseln Haftung Gewährleistung Kunden Käufer
    Lieferung Zahlung Rechnung Widerruf Widerrufsrecht Bestellung Ware Waren Preis Preise
    Abschnitt Absatz Satz Nummer Artikel Gesetz Gesetzes Recht Rechts Gericht Gerichtsstand
    Ansprechpartner Ansprechpartnerin Geboren Geburtsdatum Kundenservice Service Support
    """
)

# Common German first names. A known first name followed by a capitalised word is a name
# even without a salutation ("die Haftung von Anna Müller"). Names that are also German
# words (Ernst, Mark, Rosa, Frieda?) are left out on purpose.
_FIRST_NAMES = _words(
    """
    Anna Maria Emma Mia Hannah Hanna Lena Lea Laura Julia Sarah Lisa Katharina Sophie Sophia
    Johanna Clara Klara Charlotte Marie Lina Ella Leonie Paula Nina Sandra Sabine Susanne
    Petra Andrea Claudia Monika Ursula Renate Brigitte Gabriele Birgit Karin Heike Kerstin
    Martina Stefanie Nicole Christina Christine Melanie Silke Anja Tanja Katrin Simone
    Daniela Jessica Jennifer Vanessa Franziska Elisabeth Erika Ingrid Helga Gisela
    Hildegard Elke Doris Jutta Bettina Manuela Barbara Angelika Anke Ines Antje Miriam
    Carina Jana Svenja Annika Larissa Michelle Selin Aylin Elif Fatma Ulrike Cornelia
    Max Maximilian Alexander Paul Leon Lukas Luca Felix Jonas Elias Noah Ben Finn Jan Tim
    Tom Niklas Moritz Julian Philipp Simon David Daniel Michael Thomas Andreas Stefan
    Stephan Christian Markus Marcus Martin Peter Klaus Jürgen Wolfgang Uwe Dieter Bernd Ralf
    Rolf Holger Jörg Torsten Thorsten Sven Dirk Matthias Sebastian Tobias Florian Johannes
    Dominik Benjamin Patrick Kevin Marcel Dennis Oliver Frank Heinz Helmut Gerhard Günter
    Horst Manfred Werner Karl Hans Joachim Rainer Norbert Volker Hartmut Lars Jens Nils
    Timo Fabian Marco Christoph Georg Heinrich Friedrich Wilhelm Hermann Mehmet Ahmet
    Mustafa Murat Erik Henrik Hendrik Konstantin Vincent Anton Emil Oskar Karsten Carsten
    Achim Bernhard Ulrich Reinhard Gerd Detlef Lothar Kai
    """
)

_SALUTATION = re.compile(
    rf"(?<![{_LOWER}{_UPPER}])(?:Herrn?|Frau|Dr\.|Prof\.)[ \t]+"
    r"(?:(?:Dr\.|Prof\.|Dipl\.-[A-Za-zäöü]+\.?)[ \t]*)*"
)
_ROLE = re.compile(
    r"(?:Geschäftsführer(?:in|innen)?|Geschäftsführung|Inhaber(?:in)?|Vorstand|"
    r"Vorstandsvorsitzende[r]?|Aufsichtsratsvorsitzende[r]?|Vorsitzende[r]?|"
    r"Ansprechpartner(?:in)?|Ansprechperson|Kontaktperson|Datenschutzbeauftragte[r]?|"
    r"Prokurist(?:in)?|Verantwortliche[r]?|vertreten durch|vertretungsberechtigt(?:e[r]?)?|"
    r"z\.\s?Hd\.?|zu Händen(?: von)?|c/o|Kontoinhaber(?:in)?|Bevollmächtigte[r]?|"
    r"Sachbearbeiter(?:in)?|Rechtsanwalt|Rechtsanwältin|Notar(?:in)?|Unterschrift|gez\.)"
    r"(?:[ \t]*\([^)\n]{0,40}\))?"  # "Verantwortlicher (§ 18 Abs. 2 MStV)"
    r"[ \t]*(?::[ \t]*|,[ \t]*|[ \t]+(?:ist|sind|wird|:)[ \t]+)?"
    r"(?:(?:Herrn?|Frau|Dr\.|Prof\.)[ \t]+)*"
)
_MAIDEN = re.compile(r"\bgeb\.[ \t]+")
_LIST_GLUE = re.compile(
    r"(?:[ \t]*,[ \t]*|[ \t]+und[ \t]+|[ \t]*/[ \t]*)(?:(?:Herrn?|Frau|Dr\.)[ \t]+)*"
)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])")

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_PERSONAL_HOSTS = re.compile(
    r"(?:^|\.)(?:linkedin|xing|facebook|instagram|twitter|x|tiktok|github|gitlab|threads|"
    r"mastodon\.social|youtube)\.[a-z]{2,}$",
    re.IGNORECASE,
)
_PERSONAL_PATH = re.compile(
    r"(?:/~|/in/|/profile?(?:/|$)|/users?(?:/|$)|/people/|/u/|/pub/|/@|/team/|/mitarbeiter|"
    r"[?&](?:e?mail|name|user|username|kunde|customer|id)=)",
    re.IGNORECASE,
)

_IBAN = re.compile(
    r"(?<![A-Z0-9])([A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?)(?![A-Z0-9])"
)
_IBAN_CONTEXT = re.compile(r"IBAN\s*:?\s*$")

_PHONE_INTERNATIONAL = re.compile(
    r"(?<![\w+])(?:\+|00)[1-9]\d{0,2}[ \t\-/.]*(?:\(0\)[ \t\-/.]*)?\(?\d{1,5}\)?"
    r"(?:[ \t\-/.]*\d{1,8}){1,5}(?!\w)"
)
# A national number needs a separator after the area code ("030 1234567", "(030) 123 45",
# "0171/1234567"), so article numbers and amounts ("0123456") stay.
_PHONE_NATIONAL = re.compile(
    r"(?<![\w.,])(?:\(0[1-9]\d{1,4}\)[ \t]*|0[1-9]\d{1,4}(?:[ \t]*[/\-][ \t]*|[ \t]+))"
    r"\d{2,8}(?:[ \t\-]\d{1,8}){0,3}(?![\w,]|\.\d)"
)
_PHONE_CONTEXT = re.compile(
    r"(?:Tel(?:efon)?|Fax|Telefax|Mobil|Handy|Mobilfunk|Phone|Hotline|Rufnummer)\.?[ \t]*"
    r"(?:\([^)\n]{0,20}\))?[ \t]*:?[ \t]*([+(\d][\d \t()/+\-.]{5,}\d)",
    re.IGNORECASE,
)

_USTID = re.compile(r"(?<![A-Z0-9])DE[ ]?\d{3}[ ]?\d{3}[ ]?\d{3}(?![\d])")
_TAX_CONTEXT = re.compile(
    r"(?:Steuer-?ID|Steuer-?IdNr\.?|Steueridentifikationsnummer|Identifikationsnummer|"
    r"Steuernummer|St\.-?Nr\.?|USt\.?-?Id(?:Nr|\.-Nr)?\.?|Umsatzsteuer-?Identifikationsnummer)"
    r"[ \t]*(?:\([^)\n]{0,30}\))?[ \t]*(?:gem(?:äß|\.)[^:\n]{0,40})?:?[ \t]*"
    r"((?:DE[ ]?)?\d[\d \t/]{7,14}\d)"
)

_REGISTER = re.compile(
    r"(?<![A-Za-z])(?:HRB|HRA|GnR|PR|VR)[ \t]*(?:Nr\.[ \t]*)?\d{1,7}(?:[ \t]?[A-Z]{1,2}(?![a-z]))?"
    r"(?!\d)"
)

_MONTHS = (
    r"(?:Januar|Jänner|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|"
    r"November|Dezember)"
)
_DATE = rf"(?:\d{{1,2}}\.[ ]?\d{{1,2}}\.[ ]?(?:19|20)?\d{{2}}|\d{{1,2}}\.[ ]?{_MONTHS}[ ]\d{{4}})"
_BIRTH = re.compile(
    r"(?:geb(?:oren)?\.?(?:[ \t]+am)?|Geburtsdatum|Geburtstag|Geb\.-Datum|\*)[ \t]*:?[ \t]*"
    rf"({_DATE})"
)

_STREET_SUFFIX = (
    r"(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|steig|pfad|"
    r"markt|promenade|kai|graben|stieg|twiete|wall)"
)
_STREET_WORD = r"(?:Straße|Strasse|Str\.|Weg|Allee|Platz|Gasse|Ring|Damm|Ufer|Chaussee|Markt)"
_STREET = (
    rf"(?:[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}]?[{_LOWER}]+)*{_STREET_SUFFIX}"
    rf"|(?:Am|An[ ]der|Im|In[ ]der|Auf[ ]dem|Zum|Zur)[ ][{_UPPER}][{_LOWER}]+"
    rf"|[{_UPPER}][{_LOWER}]+(?:er)?[ ]{_STREET_WORD})"
)
_HOUSE = r"\d{1,4}[ ]?[a-zA-Z]?(?:[ ]?[-–/][ ]?\d{1,4}[a-z]?)?(?![\d,.]\d)"
_UNITS = (
    r"(?:Euro|EUR|Prozent|Stück|Exemplare|Tage|Jahre|Monate|Stunden|Minuten|Kilometer|km|"
    r"Kilogramm|kg|Gramm|Liter|Meter|Punkte|Kunden|Mitarbeiter)"
)
_CITY = (
    rf"(?!{_UNITS}\b)[{_UPPER}][{_LOWER}]+"
    rf"(?:[ \-](?:am|an|im|bei|ob|vor)?[ \-]?[{_UPPER}][{_LOWER}]+)?"
)
_ADDRESS = re.compile(
    rf"{_STREET}[ \t]+{_HOUSE}(?:[ \t]*[,\n][ \t]*(?:D-)?\d{{5}}[ \t]+{_CITY})?"
    rf"|(?:Postfach)[ \t]+\d[\d ]{{1,10}}\d(?:[ \t]*[,\n][ \t]*(?:D-)?\d{{5}}[ \t]+{_CITY})?"
    rf"|(?<![\w.,])D-\d{{5}}[ \t]+{_CITY}"
)
_POSTCODE_CITY = re.compile(rf"(?<![\w.,])\d{{5}}[ \t]+{_CITY}")
_POSTCODE_CONTEXT = re.compile(r"(?:Anschrift|Adresse|wohnhaft|Wohnort|Sitz)\b[^\n]{0,40}$")

# Legal citations are never masked: "§ 309 Nr. 7 BGB", "§§ 305 ff. BGB", "Art. 28 Abs. 3
# lit. a DSGVO", "BGBl. I S. 2909". They are the model's most important input.
_STATUTE = re.compile(
    r"(?:§§?|Art\.|Artikel)[ \t]*\d+[a-z]?"
    r"(?:[ \t]*(?:ff?\.|Abs\.|Absatz|S\.|Satz|Nr\.|Nummer|lit\.|Buchst\.|Halbs\.|Alt\.|"
    r"Unterabs\.|UAbs\.|bis|und|,|-|–)[ \t]*[\da-z]*[a-z]?)*"
    r"(?:[ \t]+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]*(?:G|O|V|B|DSGVO|BGB|HGB|EGBGB|StGB))?"
    r"|BGBl\.[ \t]*(?:I{1,3})?[ \t]*S\.[ \t]*\d+"
)


def _iban_valid(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rotated = compact[4:] + compact[:4]
    digits = "".join(str(int(ch, 36)) for ch in rotated)
    return int(digits) % 97 == 1


def _name_run(text: str, start: int, min_tokens: int) -> tuple[int, int] | None:
    """The name starting at `start`: up to four capitalised tokens, cut at the first word
    that is not a name. None when fewer than `min_tokens` remain."""
    match = _NAME_RUN.match(text, start)
    if match is None:
        return None
    end = match.start()
    count = 0
    for token in _TOKEN.finditer(text, match.start(), match.end()):
        if token.group(0) in _NOT_A_NAME:
            break
        count += 1
        end = token.end()
    if count < min_tokens:
        return None
    return match.start(), end


def _persons(text: str) -> Iterable[_Span]:
    def with_list(start: int, min_tokens: int) -> Iterable[_Span]:
        run = _name_run(text, start, min_tokens)
        while run is not None:
            yield _Span(run[0], run[1], "PERSON")
            glue = _LIST_GLUE.match(text, run[1])
            if glue is None:
                return
            run = _name_run(text, glue.end(), 2)

    for match in _SALUTATION.finditer(text):
        yield from with_list(match.end(), 1)
    for match in _ROLE.finditer(text):
        yield from with_list(match.end(), 2)
    for match in _MAIDEN.finditer(text):
        run = _name_run(text, match.end(), 1)
        if run is not None:
            yield _Span(run[0], run[1], "PERSON")
    for token in _TOKEN.finditer(text):
        if token.group(0) in _FIRST_NAMES:
            run = _name_run(text, token.start(), 2)
            if run is not None:
                yield _Span(run[0], run[1], "PERSON")


def _urls(text: str) -> Iterable[_Span]:
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        rest = re.sub(r"^(?:https?://)?", "", url, flags=re.IGNORECASE)
        host, _, path = rest.partition("/")
        path = "/" + path if path else ""
        social = _PERSONAL_HOSTS.search(host.split(":")[0]) and path.strip("/")
        if social or _PERSONAL_PATH.search(path):
            yield _Span(match.start(), match.start() + len(url), "URL")


def _phones(text: str) -> Iterable[_Span]:
    for pattern in (_PHONE_INTERNATIONAL, _PHONE_NATIONAL):
        for match in pattern.finditer(text):
            if sum(ch.isdigit() for ch in match.group(0)) >= 6:
                yield _Span(match.start(), match.end(), "PHONE")
    for match in _PHONE_CONTEXT.finditer(text):
        number = match.group(1).rstrip(" \t-/.")
        if sum(ch.isdigit() for ch in number) >= 5:
            yield _Span(match.start(1), match.start(1) + len(number), "PHONE")


def _ibans(text: str) -> Iterable[_Span]:
    for match in _IBAN.finditer(text):
        value = match.group(1).rstrip()
        context = _IBAN_CONTEXT.search(text, max(0, match.start() - 12), match.start())
        if _iban_valid(value) or context is not None:
            yield _Span(match.start(1), match.start(1) + len(value), "IBAN")


def _tax_ids(text: str) -> Iterable[_Span]:
    for match in _USTID.finditer(text):
        yield _Span(match.start(), match.end(), "TAX_ID")
    for match in _TAX_CONTEXT.finditer(text):
        value = match.group(1).rstrip(" \t/")
        if sum(ch.isdigit() for ch in value) >= 9:
            yield _Span(match.start(1), match.start(1) + len(value), "TAX_ID")


def _registers(text: str) -> Iterable[_Span]:
    for match in _REGISTER.finditer(text):
        yield _Span(match.start(), match.end(), "REGISTER_NUMBER")


def _births(text: str) -> Iterable[_Span]:
    for match in _BIRTH.finditer(text):
        yield _Span(match.start(1), match.end(1), "DATE_OF_BIRTH")


def _addresses(text: str) -> Iterable[_Span]:
    for match in _ADDRESS.finditer(text):
        yield _Span(match.start(), match.end(), "ADDRESS")
    for match in _POSTCODE_CITY.finditer(text):
        if _POSTCODE_CONTEXT.search(text, max(0, match.start() - 60), match.start()):
            yield _Span(match.start(), match.end(), "ADDRESS")


def _emails(text: str) -> Iterable[_Span]:
    for match in _EMAIL.finditer(text):
        yield _Span(match.start(), match.end(), "EMAIL")


_DETECTORS: tuple[Callable[[str], Iterable[_Span]], ...] = (
    _emails,
    _urls,
    _ibans,
    _tax_ids,
    _registers,
    _phones,
    _births,
    _addresses,
    _persons,
)
# On a tie in length, the more specific kind wins.
_PRIORITY: dict[str, int] = {
    "EMAIL": 0,
    "URL": 1,
    "IBAN": 2,
    "TAX_ID": 3,
    "REGISTER_NUMBER": 4,
    "PHONE": 5,
    "DATE_OF_BIRTH": 6,
    "ADDRESS": 7,
    "PERSON": 8,
}


def _protected(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _STATUTE.finditer(text)]
    spans += [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
    return spans


_WORD = re.compile(r"\w+")
_GLUED = re.compile(r"[\w-]")


def _overlaps(span: _Span, ranges: list[tuple[int, int]]) -> bool:
    return any(span.start < end and start < span.end for start, end in ranges)


# --- Presidio NER (optional) ------------------------------------------------------------


class _NerUnavailable(Exception):
    pass


def _presidio_analyzer(nlp: Any, language: str) -> Any:
    """A Presidio analyzer over an already loaded spaCy pipeline, with only the spaCy
    recognizer for persons: everything with a shape is the regexes' job, and Presidio's
    English-centric pattern recognizers would only add false positives on German text."""
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import SpacyNlpEngine
    from presidio_analyzer.predefined_recognizers import SpacyRecognizer

    class _Loaded(SpacyNlpEngine):
        def load(self) -> None:
            self.nlp = {language: nlp}

    engine = _Loaded(models=[{"lang_code": language, "model_name": "preloaded"}])
    engine.load()
    registry = RecognizerRegistry(supported_languages=[language])
    registry.add_recognizer(
        SpacyRecognizer(supported_language=language, supported_entities=["PERSON"])
    )
    return AnalyzerEngine(registry=registry, nlp_engine=engine, supported_languages=[language])


@dataclass
class PresidioMasker:
    """German PII masking: the regex detectors above, plus Presidio NER when the spaCy
    model is installed.

    `require_ner=True` (`privacy.masking.required`) makes a missing model or Presidio an
    error (`MaskerUnavailable`, so `mask` fails and nothing is sent); otherwise the masker
    logs a warning once and runs on the regexes alone. `nlp` injects a loaded spaCy
    pipeline (tests use a blank one with an entity ruler, so no model is downloaded).
    """

    language: str = "de"
    spacy_model: str = "de_core_news_sm"
    entities: tuple[EntityKind, ...] = ENTITY_KINDS
    score_threshold: float = 0.5
    allow_list: tuple[str, ...] = ()
    require_ner: bool = False
    nlp: Any = None
    _analyzer: Any = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    remember: int = 4096
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _known: OrderedDict[str, EntityKind] = field(
        default_factory=OrderedDict, init=False, repr=False
    )

    @property
    def ner_available(self) -> bool:
        self.load()
        return self._analyzer is not None

    def load(self) -> None:
        """Load the NER layer once. Raises MaskerUnavailable when it is required and
        cannot load; otherwise a missing model degrades to regex-only with a warning."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                self._analyzer = self._build_analyzer()
            except _NerUnavailable as exc:
                if self.require_ner:
                    raise MaskerUnavailable(str(exc)) from exc
                log.warning("PII masking runs on regexes only: %s", exc)
                self._analyzer = None
            self._loaded = True

    def _build_analyzer(self) -> Any:
        nlp = self.nlp
        if nlp is None:
            if importlib.util.find_spec(self.spacy_model) is None:
                raise _NerUnavailable(
                    f"spaCy model {self.spacy_model!r} is not installed "
                    f"(uv pip install {self.spacy_model} from explosion/spacy-models)"
                )
            try:
                import spacy

                nlp = spacy.load(self.spacy_model, exclude=["parser", "lemmatizer"])
            except Exception as exc:  # noqa: BLE001 - any load fault means no NER
                raise _NerUnavailable(f"spaCy model failed to load: {exc}") from exc
        try:
            return _presidio_analyzer(nlp, self.language)
        except ImportError as exc:
            raise _NerUnavailable(f"Presidio is not installed: {exc}") from exc

    def detect(self, text: str) -> list[_Span]:
        """The spans to mask, non-overlapping and in text order."""
        self.load()
        wanted = set(self.entities)
        protected = _protected(text)
        allowed = {word.casefold() for word in self.allow_list}

        candidates: list[_Span] = []
        for detector in _DETECTORS:
            candidates.extend(detector(text))
        if self._analyzer is not None and "PERSON" in wanted:
            for result in self._analyzer.analyze(
                text=text,
                language=self.language,
                entities=["PERSON"],
                score_threshold=self.score_threshold,
            ):
                candidates.append(_Span(result.start, result.end, "PERSON"))

        def keep(span: _Span) -> bool:
            if span.kind not in wanted or span.end <= span.start:
                return False
            if _overlaps(span, protected):
                return False
            words = re.findall(r"[\wÄÖÜäöüß.-]+", text[span.start : span.end])
            return not (allowed and all(w.casefold().strip(".") in allowed for w in words))

        candidates = [span for span in candidates if keep(span)]
        candidates += self._repeats(text, candidates, protected)

        chosen: list[_Span] = []
        for span in sorted(
            set(candidates), key=lambda s: (-(s.end - s.start), _PRIORITY[s.kind], s.start)
        ):
            if not any(span.start < c.end and c.start < span.end for c in chosen):
                chosen.append(span)
        return sorted(chosen)

    def _repeats(
        self, text: str, found: list[_Span], protected: list[tuple[int, int]]
    ) -> list[_Span]:
        """Every other mention of a value detected here or recently elsewhere.

        A context finds "Geschäftsführer: Anna Müller" once; the clause may say "Frau
        Müller" or "Müller" later without one, and the model's answer, traced on its own
        by the Langfuse hook, may say "Anna Müller" with no context at all. So the masker
        remembers (in memory only, the last `remember` values, never stored or logged)
        what it detected, and masks those values wherever they recur.
        """
        allowed = {word.casefold() for word in self.allow_list}
        values: dict[str, EntityKind] = {}
        for span in found:
            value = text[span.start : span.end]
            values[value] = span.kind
            if span.kind == "PERSON":
                surname = value.split()[-1]
                if len(surname) >= 3 and surname not in _NOT_A_NAME:
                    values.setdefault(surname, "PERSON")
        values = {
            value: kind
            for value, kind in values.items()
            if len(value) >= 3 and value.casefold() not in allowed
        }
        known = self._remember(values)
        # Index by each value's first run of word characters, then check the candidates at
        # every word of the text: linear in the text, whatever the memory holds.
        by_word: dict[str, list[tuple[str, int, EntityKind]]] = {}
        for value, kind in known.items():
            first = _WORD.search(value)
            if first is not None:
                by_word.setdefault(first.group(0), []).append((value, first.start(), kind))
        extra: list[_Span] = []
        for word in _WORD.finditer(text):
            for value, offset, kind in by_word.get(word.group(0), ()):
                start = word.start() - offset
                end = start + len(value)
                if start < 0 or not text.startswith(value, start):
                    continue
                if _GLUED.match(text[start - 1 : start]) or _GLUED.match(text[end : end + 1]):
                    continue
                span = _Span(start, end, kind)
                if not _overlaps(span, protected):
                    extra.append(span)
        return extra

    def _remember(self, values: dict[str, EntityKind]) -> dict[str, EntityKind]:
        """Add `values` to the memory; return everything to look for in this text."""
        with self._lock:
            for value, kind in values.items():
                self._known[value] = kind
                self._known.move_to_end(value)
            while len(self._known) > max(self.remember, 0):
                self._known.popitem(last=False)
            return {**self._known, **values}

    def mask(self, text: str) -> MaskedText:
        spans = self.detect(text)
        if not spans:
            return MaskedText(text)
        # Placeholders already in the text (a quoted earlier answer) keep their meaning:
        # new numbers start after them, so `unmask` never rewrites a literal one.
        used: dict[str, int] = {}
        for match in PLACEHOLDER.finditer(text):
            used[match.group(1)] = max(used.get(match.group(1), 0), int(match.group(2)))
        by_value: dict[tuple[str, str], str] = {}
        placeholders: dict[str, str] = {}
        parts: list[str] = []
        cursor = 0
        for span in spans:
            value = text[span.start : span.end]
            key = (span.kind, value)
            token = by_value.get(key)
            if token is None:
                used[span.kind] = used.get(span.kind, 0) + 1
                token = placeholder(span.kind, used[span.kind])
                by_value[key] = token
                placeholders[token] = value
            parts.append(text[cursor : span.start])
            parts.append(token)
            cursor = span.end
        parts.append(text[cursor:])
        return MaskedText("".join(parts), placeholders)


# --- the boundary -----------------------------------------------------------------------


@dataclass
class MaskingBoundary:
    """The one place outbound text is masked. `masker=None` means masking is off and text
    passes unchanged (the pre-Phase 9 behaviour, and the default until 9.A's gate)."""

    masker: Masker | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def enabled(self) -> bool:
        return self.masker is not None

    def outbound(self, text: str) -> str:
        """The text to send: masked when enabled (a `MaskedText`), else `text` itself.

        Raises MaskingError if the masker fails; callers turn that into a failed result
        without sending anything. Text that is already a `MaskedText` is not masked twice.
        """
        masker = self.masker
        if masker is None or isinstance(text, MaskedText):
            return text
        try:
            masked = masker.mask(text)
        except NotImplementedError:
            raise
        except Exception as exc:  # noqa: BLE001 - any masker fault blocks the send
            raise MaskingError(f"masking failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(masked, MaskedText):
            raise MaskingError("masker returned an unmarked string")
        return masked

    def require_masked(self, *values: Any) -> None:
        """Refuse (UnmaskedPayload) any `str` among `values` that did not pass `outbound`,
        while masking is enabled. Strict fake clients call this on every payload."""
        if not self.enabled:
            return
        for value in values:
            if isinstance(value, str) and not isinstance(value, MaskedText):
                raise UnmaskedPayload("unmasked text reached a provider client")
            if isinstance(value, list | tuple):
                self.require_masked(*value)

    def langfuse_mask(self, *, data: Any, **_: Any) -> Any:
        """Langfuse's `mask=` hook (`langfuse.types.MaskFunction`): `data` with every string
        inside it masked, recursively through dicts (keys too), lists, pydantic models and
        dataclasses; anything else is masked as its `str`. Masking off: `data` unchanged.

        A masker failure raises MaskingError; Langfuse then records its own fallback
        ("fully masked") instead of the data, so a failure never leaks.
        """
        if not self.enabled:
            return data
        return self._walk(data)

    def _walk(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return str(self.outbound(value))
        if isinstance(value, Mapping):
            return {
                (self._walk(key) if isinstance(key, str) else key): self._walk(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple | set | frozenset):
            return [self._walk(item) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return self._walk(dump(mode="json"))
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._walk(dataclasses.asdict(value))
        return self._walk(str(value))

    def trace_text(self, text: str | None) -> str | None:
        """`text` masked for a Langfuse call the `mask=` hook does not cover (a score
        comment). A masker failure drops the text (None) rather than sending it."""
        if text is None or not self.enabled:
            return text
        try:
            return str(self.outbound(text))
        except MaskingError:
            log.warning("a trace comment could not be masked and is left out")
            return None


_BOUNDARY = MaskingBoundary()


def boundary() -> MaskingBoundary:
    """The process-wide boundary every outbound call uses."""
    return _BOUNDARY


def configure(masker: Masker | None) -> MaskingBoundary:
    """Set (or clear, with None) the process-wide masker; returns the boundary."""
    with _BOUNDARY._lock:
        _BOUNDARY.masker = masker
    return _BOUNDARY


def masker_from_config(config: Mapping[str, Any]) -> Masker | None:
    """The masker `privacy.masking` in configs/app.yaml asks for; None when disabled."""
    block = (config.get("privacy") or {}).get("masking") or {}
    if not block.get("enabled", False):
        return None
    return PresidioMasker(
        language=str(block.get("language", "de")),
        spacy_model=str(block.get("spacy_model", "de_core_news_sm")),
        entities=tuple(block.get("entities") or ENTITY_KINDS),
        score_threshold=float(block.get("score_threshold", 0.5)),
        allow_list=tuple(block.get("allow_list") or ()),
        require_ner=bool(block.get("required", False)),
    )


# --- streaming: unmask a model's answer piece by piece -----------------------------------

_PARTIAL = re.compile(r"<[A-Z_]{0,16}\d{0,6}$")
_PREFIXES = tuple(f"<{kind}_" for kind in ENTITY_KINDS)


class StreamUnmasker:
    """Restores placeholders in a streamed answer. A placeholder can be split across two
    pieces ("<PERS" + "ON_1> sagt"), so a tail that could still become one is held back
    until the next piece (or `flush`) decides."""

    def __init__(self, masked: MaskedText) -> None:
        self._masked = masked
        self._pending = ""

    def feed(self, piece: str) -> str:
        buffer = self._pending + piece
        match = _PARTIAL.search(buffer)
        if match is not None and any(
            prefix.startswith(match.group(0)) or match.group(0).startswith(prefix)
            for prefix in _PREFIXES
        ):
            self._pending = match.group(0)
            buffer = buffer[: match.start()]
        else:
            self._pending = ""
        return self._masked.unmask(buffer)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._masked.unmask(rest)

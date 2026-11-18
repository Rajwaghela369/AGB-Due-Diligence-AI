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
- **Scoped, never global.** Placeholders live in a `MaskingScope`: one unit of work (one
  clause judged, one summary, one feedback comment) of one owner. A scope is primed with
  the texts the work is about (the clause), so every string masked inside it, including
  a model's answer traced on its own, is masked from that map and numbered the same way.
  The scope ends with the work; nothing is remembered across units or owners, so one
  owner's data can never change another's prompts, and erasure (9.B) has nothing to
  chase in memory. Outside a scope every call gets a fresh one.
- **Reversible, for display only.** `MaskedText.placeholders` maps each placeholder
  (`<PERSON_1>`) back to the original; it is never stored or traced. `core.llm` restores a
  model's answer before returning it, and only from the values the scope allows
  (`restore_from`): a verdict is cached for every owner with the same clause, so its
  reasoning may name only what that clause itself says.
- **Cache identity stays on the unmasked text.** `core.hashing.content_hash` runs on the
  original clause, so a verdict cached before masking is still found after it.

One module-level boundary, configured once at start-up (`configure`), rather than a masker
threaded through every call: `core.llm` is called from the agent graph, the eval harness and
the review service, and a global boundary is the only way none of them can skip it. Tests
reset it with `configure(None)`.

Detection (`PresidioMasker`) runs in two layers. German regexes find everything with a
fixed shape or a telling context: e-mail, phone, IBAN (checksum or account context), tax
and register numbers, dates of birth, street addresses, URLs with personal paths, and names
after a salutation, a label ("Geschäftsführer:", also across a line break), a common first
name, above an address or under a signature date. Presidio with a German spaCy model adds
named-entity recognition for names without such a context. Without the model the masker
refuses to work (`MaskerUnavailable`) unless `allow_regex_only` says the deployment accepts
the regex layer's known gaps. Statute citations (`§ 309 Nr. 7 BGB`) and existing
placeholders are protected; a detection that only touches a citation keeps its part
outside it.
"""

from __future__ import annotations

import bisect
import contextvars
import dataclasses
import importlib.util
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from core.logging import get_logger

log = get_logger("core.masking")

# What 9.A masks. Placeholders are `<KIND_n>`, numbered per scope in order of first
# appearance, so the same value twice is one placeholder.
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
# What a reader sees where a placeholder cannot (or may not) be restored.
UNKNOWN = "[unbekannt]"


def placeholder(kind: EntityKind, number: int) -> str:
    return f"<{kind}_{number}>"


class MaskedText(str):
    """A string that went through the masker, with the way back.

    Being a `str`, it goes wherever the text went before (a request body, a prompt), so no
    provider call changes shape. Any operation that builds a new string (`+`, `format`,
    slicing) returns a plain `str`: the marker survives only on exactly what was masked.
    """

    placeholders: Mapping[str, str]
    restorable: frozenset[str] | None

    def __new__(
        cls,
        text: str,
        placeholders: Mapping[str, str] | None = None,
        restorable: Iterable[str] | None = None,
    ) -> MaskedText:
        masked = super().__new__(cls, text)
        masked.placeholders = dict(placeholders or {})
        masked.restorable = frozenset(restorable) if restorable is not None else None
        return masked

    def unmask(self, text: str | None = None) -> str:
        """`text` (a model's answer quoting placeholders; this text when None) with every
        known placeholder replaced by its original. Unknown placeholders stay as they are."""
        source = str(self) if text is None else text
        if not self.placeholders:
            return source
        return PLACEHOLDER.sub(lambda m: self.placeholders.get(m.group(0), m.group(0)), source)

    def display(self, text: str) -> str:
        """A model's answer for a reader: known placeholders restored when the scope allows
        their value (`restorable`), everything else (a placeholder the model invented, or
        one whose value may not be restored here) shown as `[unbekannt]`."""

        def restore(match: re.Match[str]) -> str:
            token = match.group(0)
            value = self.placeholders.get(token)
            if value is None or (self.restorable is not None and token not in self.restorable):
                return UNKNOWN
            return value

        return PLACEHOLDER.sub(restore, text)


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
    """The masker cannot run as configured: Presidio or the spaCy model is missing and
    `allow_regex_only` is not set. `build_app` lets this stop the start-up."""


class IdentityMasker:
    """Masks nothing but marks the text as having passed the boundary. For tests of the
    plumbing, and for a deployment that has decided (documented) to send text as is."""

    def mask(self, text: str) -> MaskedText:
        return MaskedText(text)


# --- detection: German patterns ---------------------------------------------------------
#
# Each detector yields `_Span`s; `PresidioMasker.detect` trims them away from protected
# spans, drops allow-listed ones and resolves overlaps (longest first). The patterns lean
# towards precision on legal prose: a false positive in a clause changes what the model
# judges, so shapes that also occur in statutes (bare five-digit numbers, dates,
# capitalised nouns) are only masked with a context that makes them personal.


@dataclass(frozen=True, order=True)
class _Span:
    start: int
    end: int
    kind: EntityKind


# Latin letters with the diacritics of names common in Germany (Turkish, Polish, Nordic,
# French): "Yıldırım", "Sørensen", "Kowalczyk", "Łukasz".
_UPPER = "A-ZÀ-ÖØ-ÞĄĆČĘŁŃŘŚŠŞŻŽĞİ"
_LOWER = "a-zß-öø-ÿąćčęłńřśšşżžğı"
_WORD_END = rf"(?![{_LOWER}{_UPPER}\w])"

# A name token: "Müller", "Müller-Lüdenscheidt". Not "GmbH" (the boundary forbids "Gmb|H").
_NAME_TOKEN = rf"[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)*{_WORD_END}"
_PARTICLE = r"(?:von|van|de|zu|vom|zur|di|da|ten|del)(?:\s+(?:der|den|dem))?"
_NAME_RUN = re.compile(rf"{_NAME_TOKEN}(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_NAME_TOKEN}){{0,3}}")
_TOKEN = re.compile(_NAME_TOKEN)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Sentence-initial function words: a run followed by one is title-case prose ("Notar
# Kosten Trägt Der Käufer"), not a name.
_FUNCTION_WORDS = _words(
    """
    Der Die Das Den Dem Des Ein Eine Einen Einem Einer Eines Sie Er Es Wir Ihr Ihre Ihnen
    Unser Unsere Diese Dieser Dieses Alle Jede Jeder Jedes Kein Keine Bei Mit Von Für Nach
    Und Oder Im In Am An Auf Aus Zum Zur Ist Sind Wird Werden Hat Haben Als Wenn Soweit
    """
)
# Capitalised words that end a name run: function words and the nouns that follow a name
# in an imprint ("Anna Müller Telefon 030 …").
_NOT_A_NAME = _FUNCTION_WORDS | _words(
    """
    Telefon Tel Fax Telefax Mobil Handy Mail Email Web Internet Website Homepage Adresse
    Anschrift Sitz Registergericht Amtsgericht Handelsregister Registernummer Steuernummer
    Umsatzsteuer Gesellschaft Firma Unternehmen Straße Strasse Postfach Geschäftsführer
    Geschäftsführerin Geschäftsführung Inhaber Inhaberin Vorstand Kontakt Bankverbindung
    Bank Kreditinstitut Sparkasse Volksbank Datenschutz Datenschutzbeauftragter Hinweis
    Vertrag Vertrags Verträge Klausel Klauseln Haftung Gewährleistung Kunden Käufer
    Lieferung Zahlung Rechnung Widerruf Widerrufsrecht Bestellung Ware Waren Preis Preise
    Abschnitt Absatz Satz Nummer Artikel Gesetz Gesetzes Recht Rechts Gericht Gerichtsstand
    Ansprechpartner Ansprechpartnerin Geboren Geburtsdatum Kundenservice Service Support
    Prokurist Prokuristin Verantwortlich Verantwortlicher
    """
)
# Words that make a capitalised run a company or an institution, not a person.
_COMPANY = re.compile(
    r"(?:[ \t]+[A-ZÄÖÜ][\w&.\-]*){0,2}[ \t]+(?:GmbH|AG|KG|OHG|UG|SE|GbR|KGaA|mbH|"
    r"e\.\s?K\.|e\.\s?V\.|Institut|Stiftung|Verlag|Gruppe|Holding|&\s?Co)(?![\w])"
)
_COMPANY_WORDS = _words("Institut Stiftung Verlag Gruppe Holding Baumarkt Werke Bank")

# Common German first names. A known first name followed by a capitalised word is a name
# even without a salutation ("die Haftung von Anna Müller"). Names that are also German
# words (Ernst, Mark, Rosa) are left out on purpose.
_FIRST_NAMES = _words(
    """
    Anna Maria Emma Mia Hannah Hanna Lena Lea Laura Julia Sarah Lisa Katharina Sophie Sophia
    Johanna Clara Klara Charlotte Marie Lina Ella Leonie Paula Nina Sandra Sabine Susanne
    Petra Andrea Claudia Monika Ursula Renate Brigitte Gabriele Birgit Karin Heike Kerstin
    Martina Stefanie Nicole Christina Christine Melanie Silke Anja Tanja Katrin Simone
    Daniela Jessica Jennifer Vanessa Franziska Elisabeth Erika Ingrid Helga Gisela
    Hildegard Elke Doris Jutta Bettina Manuela Barbara Angelika Anke Ines Antje Miriam
    Carina Jana Svenja Annika Larissa Michelle Selin Aylin Elif Fatma Ulrike Cornelia
    Lieselotte Gertrud Waltraud Hannelore Ilse Edith Inge Gundula Margarete Dorothea
    Max Maximilian Alexander Paul Leon Lukas Luca Felix Jonas Elias Noah Ben Finn Jan Tim
    Tom Niklas Moritz Julian Philipp Simon David Daniel Michael Thomas Andreas Stefan
    Stephan Christian Markus Marcus Martin Peter Klaus Jürgen Wolfgang Uwe Dieter Bernd Ralf
    Rolf Holger Jörg Torsten Thorsten Sven Dirk Matthias Sebastian Tobias Florian Johannes
    Dominik Benjamin Patrick Kevin Marcel Dennis Oliver Frank Heinz Helmut Gerhard Günter
    Horst Manfred Werner Karl Hans Joachim Rainer Norbert Volker Hartmut Lars Jens Nils
    Timo Fabian Marco Christoph Georg Heinrich Friedrich Wilhelm Hermann Mehmet Ahmet
    Mustafa Murat Erik Henrik Hendrik Konstantin Vincent Anton Emil Oskar Karsten Carsten
    Achim Bernhard Ulrich Reinhard Gerd Detlef Lothar Kai Björn Jörn Sören Malte Gundolf
    """
)

_TITLES = r"(?:(?:Herrn?|Frau|Dr\.|Prof\.|RA|RAin|Dipl\.-[A-Za-zäöü]+\.?)[ \t]+)*"
_SALUTATION = re.compile(
    rf"(?<![{_LOWER}{_UPPER}])(?:Herrn?|Frau|Dr\.|Prof\.)[ \t]+"
    r"(?:(?:Dr\.|Prof\.|RA|RAin|Dipl\.-[A-Za-zäöü]+\.?)[ \t]*)*"
)
# A role or label, then its separator, which may be a line break ("Geschäftsführer:\n…").
_ROLE = re.compile(
    r"(?:Geschäftsführer(?:in|innen)?|Geschäftsführung|Inhaber(?:in)?|Vorstand|"
    r"Vorstandsvorsitzende[r]?|Aufsichtsratsvorsitzende[r]?|Vorsitzende[r]?|"
    r"Ansprechpartner(?:in)?|Ansprechperson|Kontaktperson|Kontakt|Datenschutzbeauftragte[r]?|"
    r"Prokurist(?:in)?|Verantwortlich(?:e[r]?)?|vertreten durch|vertretungsberechtigt(?:e[r]?)?|"
    r"z\.\s?Hd\.?|zu Händen(?: von)?|c/o|Kontoinhaber(?:in)?|Bevollmächtigte[r]?|"
    r"Sachbearbeiter(?:in)?|Rechtsanwalt|Rechtsanwältin|Notar(?:in)?|Unterschrift|gez\.)"
    # "Verantwortlicher (§ 18 Abs. 2 MStV)", "Verantwortlich i.S.d. § 18 MStV"
    r"(?:[ \t]*\([^)\n]{0,40}\)|[ \t]+(?:i\.\s?S\.\s?[dv]\.|gem(?:äß|\.)|nach)[^:\n]{0,40}(?=:))?"
    rf"(?:[ \t]*:\s*|[ \t]*,\s*|[ \t]+(?:ist|sind|wird)\s+|\s+){_TITLES}"
)
_MAIDEN = re.compile(r"\bgeb\.[ \t]+")
_LOWER_SALUTATION = re.compile(
    rf"(?<![\w])(?:herrn?|frau)[ \t]+([{_LOWER}][{_LOWER}\-]{{2,}}){_WORD_END}"
)
_LIST_GLUE = re.compile(rf"(?:[ \t]*,[ \t]*|[ \t]+und[ \t]+|[ \t]*/[ \t]*){_TITLES}")
_SIGNATURE_LINE = re.compile(
    r"(?:,[ \t]*den[ \t]+\d{1,2}\.[ \t]?\d{1,2}\.[ \t]?\d{2,4}|,[ \t]*\d{1,2}\.[ \t]?\d{1,2}\."
    r"[ \t]?\d{4})[ \t]*\n[ \t]*$"
)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w-])")
_EMAIL_OBFUSCATED = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+[ \t]*[(\[][ \t]*(?:at|ät)[ \t]*[)\]][ \t]*[A-Za-z0-9-]+"
    r"(?:(?:[ \t]*[(\[][ \t]*(?:dot|punkt)[ \t]*[)\]][ \t]*|\.)[A-Za-z0-9-]+)+",
    re.IGNORECASE,
)

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
    r"(?<![A-Za-z0-9])([A-Za-z]{2}\d{2}(?:[ ]?[A-Za-z0-9]{4}){2,7}(?:[ ]?[A-Za-z0-9]{1,3})?)"
    r"(?![A-Za-z0-9])"
)
_IBAN_CONTEXT = re.compile(
    r"(?:IBAN|Konto(?:nummer|-Nr\.?)?|Bankverbindung|Kontoverbindung)[ \t]*:?[ \t]*$",
    re.IGNORECASE,
)

_PHONE_INTERNATIONAL = re.compile(
    r"(?<![\w+])(?:\+|00)[1-9]\d{0,2}[ \t\-/.]*(?:\(0\)[ \t\-/.]*)?\(?\d{1,5}\)?"
    r"(?:[ \t\-/.]*\d{1,8}){1,5}(?!\w)"
)
# A national number needs a separator after the area code ("030 1234567", "(030) 123 45",
# "0171/1234567"), so article numbers and amounts ("0123456") stay; without one it needs
# the length of a real number (10 to 13 digits).
_PHONE_NATIONAL = re.compile(
    r"(?<![\w.,/])(?:\(0[1-9]\d{1,4}\)[ \t]*|0[1-9]\d{1,4}(?:[ \t]*[/\-][ \t]*|[ \t]+))"
    r"\d{2,8}(?:[ \t\-]\d{1,8}){0,3}(?![\w,]|\.\d)"
    r"|(?<![\w.,/])0[1-9]\d{8,11}(?![\w,]|\.\d)"
)
_PHONE_CONTEXT = re.compile(
    r"(?:Tel(?:efon)?|Fax|Telefax|Mobil|Handy|Mobilfunk|Phone|Hotline|Rufnummer)\.?[ \t]*"
    r"(?:\([^)\n]{0,20}\))?[ \t]*:?[ \t]*([+(\d][\d \t()/+\-.]{5,24}\d)",
    re.IGNORECASE,
)
# "Bestellnummer 0049 123456" is an order number, "Telefonnummer 030 …" a phone.
_NUMBER_LABEL = re.compile(r"(\w*)(?:nummer|nr\.?)[ \t]*:?[ \t]*$", re.IGNORECASE)
_PHONE_LABELS = ("tel", "ruf", "fax", "handy", "mobil", "kontakt", "service", "hotline")

_USTID = re.compile(r"(?<![A-Z0-9])DE[ ]?\d{3}[ ]?\d{3}[ ]?\d{3}(?![\d])")
_TAX_CONTEXT = re.compile(
    r"(?:Steuer-?ID|Steuer-?IdNr\.?|Steueridentifikationsnummer|Identifikationsnummer|"
    r"Steuernummer|St\.-?Nr\.?|USt\.?-?Id(?:Nr|\.-Nr)?\.?|Umsatzsteuer-?Identifikationsnummer)"
    r"[ \t]*(?:\([^)\n]{0,30}\))?[ \t]*(?:gem(?:äß|\.)[^:\n]{0,40})?:?[ \t]*"
    r"((?:DE[ ]?)?\d[\d \t/]{7,14}\d)"
)

_REGISTER = re.compile(
    r"(?<![A-Za-z])(HRB|HRA|GnR|PR|VR)[ \t]*(?:Nr\.[ \t]*)?(\d{1,7})"
    r"(?:[ \t]?[A-Z]{1,2}(?![a-z]))?(?!\d)"
)
_REGISTER_CONTEXT = re.compile(
    r"(?:Amtsgericht|Registergericht|Register|registergericht|register|AG)\b[^\n]{0,40}$"
)
_REGISTER_LABELLED = re.compile(
    r"(?:Handelsregister|Registernummer|Register-Nr\.?|Vereinsregister|"
    r"Partnerschaftsregister|Genossenschaftsregister)[ \t]*:?[^\n\d]{0,30}?"
    r"(?:Nr\.[ \t]*)?(\d{2,7}(?:[ \t]?[A-Z]{1,2}(?![a-z]))?)(?!\d)"
)

_MONTHS = (
    r"(?:Januar|Jänner|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|"
    r"November|Dezember)"
)
_DATE = (
    rf"(?:\d{{1,2}}\.[ ]?\d{{1,2}}\.[ ]?(?:19|20)?\d{{2}}|\d{{1,2}}\.[ ]?{_MONTHS}[ ]\d{{4}}"
    r"|(?:19|20)\d{2}-[01]\d-[0-3]\d)"
)
_BIRTH = re.compile(
    r"(?:geb(?:oren)?\.?(?:[ \t]+am)?|Geburtsdatum|Geburtstag|Geb\.-Datum|\*)[ \t]*:?[ \t]*"
    rf"({_DATE})"
)

_STREET_SUFFIX = (
    r"(?:straße|strasse|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|steig|pfad|"
    r"markt|promenade|kai|graben|stieg|twiete|wall)"
)
_STREET_WORD = r"(?:Straße|Strasse|Str\.|Weg|Allee|Platz|Gasse|Ring|Damm|Ufer|Chaussee|Markt)"
_STREET_PREFIX = (
    r"(?:Am|An[ ]der|An[ ]den|Im|In[ ]der|In[ ]den|Auf[ ]dem|Auf[ ]der|Zum|Zur|"
    r"Unter[ ]den|Unter[ ]der|Hinter[ ]dem|Hinter[ ]der|Vor[ ]dem|Neben[ ]der|Über[ ]den)"
)
_STREET = (
    rf"(?:[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}]?[{_LOWER}]+)*{_STREET_SUFFIX}"
    rf"|[{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)*-{_STREET_WORD}"
    rf"|{_STREET_PREFIX}[ ][{_UPPER}][{_LOWER}]+"
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
    rf"{_STREET}[ \t]+{_HOUSE}(?:[ \t]*(?:,|\n)[ \t]*(?:D-)?\d{{5}}[ \t]+{_CITY})?"
    rf"|(?:Postfach)[ \t]+\d[\d ]{{1,10}}\d(?:[ \t]*(?:,|\n)[ \t]*(?:D-)?\d{{5}}[ \t]+{_CITY})?"
    rf"|(?<![\w.,])D-\d{{5}}[ \t]+{_CITY}"
)
_POSTCODE_CITY = re.compile(rf"(?<![\w.,])\d{{5}}[ \t]+{_CITY}")
_POSTCODE_CONTEXT = re.compile(r"(?:Anschrift|Adresse|wohnhaft|Wohnort|Sitz)\b[^\n]{0,40}$")
_LINE_OF_NAME = re.compile(rf"[ \t]*({_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{1,2}})[ \t]*\n[ \t]*$")

# Legal citations are never masked: "§ 309 Nr. 7 BGB", "§§ 305 ff. BGB", "Art. 28 Abs. 3
# lit. a DSGVO", "BGBl. I S. 2909". A list continuation (", 4", "bis 310", "und 5") is a
# section number (no leading zero, at most four digits) followed by more citation or the
# end of the citation, so a phone number or a postcode after a citation stays outside it.
_LAW = r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]*(?:G|O|V|B|DSGVO|BGB|HGB|EGBGB|StGB)\b"
_SECTION = r"[1-9]\d{0,3}[a-z]?"
_STATUTE = re.compile(
    rf"(?:§§?|Art\.|Artikel)[ \t]*{_SECTION}"
    r"(?:[ \t]*(?:"
    r"(?:Abs\.|Absatz|S\.|Satz|Nr\.|Nummer|Halbs\.|Alt\.|Unterabs\.|UAbs\.)[ \t]*[1-9]\d{0,3}[a-z]?"
    r"|(?:lit\.|Buchst\.)[ \t]*[a-z]{1,2}\)?"
    r"|ff?\."
    rf"|(?:,|und|bis|-|–)[ \t]*{_SECTION}(?=[ \t]*(?:[,;.)]|und\b|bis\b|ff|Abs|Satz|S\.|Nr|"
    rf"lit|{_LAW}|$))"
    r"))*"
    rf"(?:[ \t]+{_LAW})?"
    r"|BGBl\.[ \t]*(?:I{1,3})?[ \t]*S\.[ \t]*\d+"
)


def _iban_valid(candidate: str) -> bool:
    compact = candidate.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34 or not compact[:2].isalpha():
        return False
    rotated = compact[4:] + compact[:4]
    try:
        digits = "".join(str(int(ch, 36)) for ch in rotated)
    except ValueError:
        return False
    return int(digits) % 97 == 1


def _name_run(text: str, start: int, min_tokens: int) -> tuple[int, int] | None:
    """The name starting at `start`: up to four capitalised tokens, cut at the first word
    that is not a name. None when fewer than `min_tokens` remain, or when the run is
    title-case prose (a function word follows it, "Kosten Trägt Der Käufer")."""
    match = _NAME_RUN.match(text, start)
    if match is None:
        return None
    end = match.start()
    count = 0
    stopped_by: str | None = None
    for token in _TOKEN.finditer(text, match.start(), match.end()):
        if token.group(0) in _NOT_A_NAME:
            stopped_by = token.group(0)
            break
        count += 1
        end = token.end()
    if count < min_tokens:
        return None
    if stopped_by is None:
        gap = end
        while gap < len(text) and text[gap] in " \t":
            gap += 1
        following = _TOKEN.match(text, gap)
        stopped_by = following.group(0) if following is not None else None
    if stopped_by in _FUNCTION_WORDS and min_tokens > 1:
        return None
    return match.start(), end


def _is_company(text: str, start: int, end: int) -> bool:
    tokens = text[start:end].split()
    return any(token in _COMPANY_WORDS for token in tokens) or bool(_COMPANY.match(text, end))


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
    # Typed in lower case ("Ansprechpartner ist herr müller"): only after a lower-case
    # salutation, which never starts a sentence of legal prose.
    for match in _LOWER_SALUTATION.finditer(text):
        yield _Span(match.start(1), match.end(1), "PERSON")
    for match in _MAIDEN.finditer(text):
        run = _name_run(text, match.end(), 1)
        if run is not None:
            yield _Span(run[0], run[1], "PERSON")
    for token in _TOKEN.finditer(text):
        if token.group(0) in _FIRST_NAMES:
            run = _name_run(text, token.start(), 2)
            if run is not None and not _is_company(text, *run):
                yield _Span(run[0], run[1], "PERSON")
    # A signature: a name alone on the line after "Ort, den 1.1.2024".
    for match in re.finditer(
        rf"^[ \t]*({_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{1,2}})[ \t]*$", text, re.MULTILINE
    ):
        before = text[max(0, match.start() - 60) : match.start()]
        if _SIGNATURE_LINE.search(before) and not _is_company(text, *match.span(1)):
            tokens = match.group(1).split()
            if not any(token in _NOT_A_NAME for token in tokens):
                yield _Span(match.start(1), match.end(1), "PERSON")


def _persons_above(text: str, addresses: list[_Span]) -> Iterable[_Span]:
    """A name block: a line of two or three name tokens directly above an address."""
    for address in addresses:
        line_start = text.rfind("\n", 0, address.start) + 1
        if text[line_start : address.start].strip():
            continue  # the address does not start its line
        match = _LINE_OF_NAME.search(text, max(0, line_start - 80), line_start)
        if match is None or match.end() != line_start:
            continue
        start, end = match.span(1)
        tokens = text[start:end].split()
        if any(token in _NOT_A_NAME for token in tokens) or _is_company(text, start, end):
            continue
        yield _Span(start, end, "PERSON")


def _urls(text: str) -> Iterable[_Span]:
    for match in _URL.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        rest = re.sub(r"^(?:https?://)?", "", url, flags=re.IGNORECASE)
        host, _, path = rest.partition("/")
        path = "/" + path if path else ""
        social = _PERSONAL_HOSTS.search(host.split(":")[0]) and path.strip("/")
        if social or _PERSONAL_PATH.search(path):
            yield _Span(match.start(), match.start() + len(url), "URL")


def _labelled_as_number(text: str, start: int) -> bool:
    label = _NUMBER_LABEL.search(text, max(0, start - 30), start)
    if label is None:
        return False
    word = label.group(1).casefold()
    return not word.startswith(_PHONE_LABELS)


def _phones(text: str) -> Iterable[_Span]:
    for pattern in (_PHONE_INTERNATIONAL, _PHONE_NATIONAL):
        for match in pattern.finditer(text):
            digits = sum(ch.isdigit() for ch in match.group(0))
            if digits >= 6 and not _labelled_as_number(text, match.start()):
                yield _Span(match.start(), match.end(), "PHONE")
    for match in _PHONE_CONTEXT.finditer(text):
        number = match.group(1).rstrip(" \t-/.")
        if sum(ch.isdigit() for ch in number) >= 5:
            yield _Span(match.start(1), match.start(1) + len(number), "PHONE")


def _ibans(text: str) -> Iterable[_Span]:
    for match in _IBAN.finditer(text):
        value = match.group(1).rstrip()
        context = _IBAN_CONTEXT.search(text, max(0, match.start() - 25), match.start())
        if _iban_valid(value) or (context is not None and value[:2].isalpha()):
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
        kind, number = match.group(1), match.group(2)
        context = _REGISTER_CONTEXT.search(text, max(0, match.start() - 50), match.start())
        # "HRB 123456" is a register number on its own; "Tarifziffer HRB 5", "PR 2024" and
        # "VR 12" only next to a court or register.
        if context is not None or (kind in ("HRA", "HRB") and len(number) >= 3):
            yield _Span(match.start(), match.end(), "REGISTER_NUMBER")
    for match in _REGISTER_LABELLED.finditer(text):
        yield _Span(match.start(1), match.end(1), "REGISTER_NUMBER")


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
    for pattern in (_EMAIL, _EMAIL_OBFUSCATED):
        for match in pattern.finditer(text):
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
    return sorted(spans)


def _outside(span: _Span, protected: list[tuple[int, int]], text: str) -> _Span | None:
    """`span` without the protected parts it touches: the longest piece left outside them
    (trimmed of spaces and punctuation), or None when nothing is left."""
    pieces = [(span.start, span.end)]
    index = bisect.bisect_left(protected, (span.start, -1))
    candidates = protected[max(0, index - 1) : bisect.bisect_right(protected, (span.end, span.end))]
    for start, end in candidates:
        cut = []
        for piece_start, piece_end in pieces:
            if end <= piece_start or start >= piece_end:
                cut.append((piece_start, piece_end))
                continue
            if piece_start < start:
                cut.append((piece_start, start))
            if end < piece_end:
                cut.append((end, piece_end))
        pieces = cut
    best: tuple[int, int] | None = None
    for start, end in pieces:
        while start < end and not text[start].isalnum() and text[start] not in "+(":
            start += 1
        while end > start and not text[end - 1].isalnum():
            end -= 1
        if end > start and (best is None or end - start > best[1] - best[0]):
            best = (start, end)
    if best is None:
        return None
    if best == (span.start, span.end):
        return span
    return _Span(best[0], best[1], span.kind)


def _resolve(candidates: Iterable[_Span]) -> list[_Span]:
    """Non-overlapping spans, longest (then most specific) first, in text order. A sorted
    list of the chosen intervals keeps each check logarithmic."""
    starts: list[int] = []
    ends: list[int] = []
    chosen: list[_Span] = []
    for span in sorted(
        set(candidates), key=lambda s: (-(s.end - s.start), _PRIORITY[s.kind], s.start)
    ):
        index = bisect.bisect_left(starts, span.start)
        if index > 0 and ends[index - 1] > span.start:
            continue
        if index < len(starts) and starts[index] < span.end:
            continue
        starts.insert(index, span.start)
        ends.insert(index, span.end)
        chosen.insert(index, span)
    return chosen


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
    """German PII detection: the regex detectors above, plus Presidio NER when the spaCy
    model is installed. Stateless between calls: the same text always gives the same
    spans (`detect`), and `mask` numbers them in a fresh scope.

    Without the model (or Presidio) the masker raises `MaskerUnavailable`, so nothing is
    sent, unless `allow_regex_only` is set: then it logs one warning and runs on the
    regexes alone, accepting that a name with no context ("Die Ware holt Gundula Quast ab")
    goes through. `nlp` injects a loaded spaCy pipeline (tests use a blank one with an
    entity ruler, so no model is downloaded).
    """

    language: str = "de"
    spacy_model: str = "de_core_news_sm"
    entities: tuple[EntityKind, ...] = ENTITY_KINDS
    score_threshold: float = 0.5
    allow_list: tuple[str, ...] = ()
    allow_regex_only: bool = False
    nlp: Any = None
    _analyzer: Any = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def ner_available(self) -> bool:
        self.load()
        return self._analyzer is not None

    def load(self) -> None:
        """Load the NER layer once. Raises MaskerUnavailable when it cannot load and
        regex-only masking was not explicitly allowed."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                self._analyzer = self._build_analyzer()
            except _NerUnavailable as exc:
                if not self.allow_regex_only:
                    raise MaskerUnavailable(
                        f"{exc}; install the model or set privacy.masking.allow_regex_only"
                    ) from exc
                log.warning("PII masking runs on regexes only (allow_regex_only): %s", exc)
                self._analyzer = None
            self._loaded = True

    def _build_analyzer(self) -> Any:
        nlp = self.nlp
        if nlp is None:
            if importlib.util.find_spec(self.spacy_model) is None:
                raise _NerUnavailable(f"spaCy model {self.spacy_model!r} is not installed")
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
        """The spans to mask in `text` alone, non-overlapping and in text order."""
        self.load()
        wanted = set(self.entities)
        protected = _protected(text)

        candidates: list[_Span] = []
        for detector in _DETECTORS:
            candidates.extend(detector(text))
        candidates.extend(
            _persons_above(text, [span for span in candidates if span.kind == "ADDRESS"])
        )
        if self._analyzer is not None and "PERSON" in wanted:
            for result in self._analyzer.analyze(
                text=text,
                language=self.language,
                entities=["PERSON"],
                score_threshold=self.score_threshold,
            ):
                candidates.append(_Span(result.start, result.end, "PERSON"))

        kept: list[_Span] = []
        for span in candidates:
            if span.kind not in wanted or span.end <= span.start:
                continue
            trimmed = _outside(span, protected, text) if protected else span
            if trimmed is not None and not self._allowed(text[trimmed.start : trimmed.end]):
                kept.append(trimmed)
        return _resolve(kept)

    def _allowed(self, value: str) -> bool:
        if not self.allow_list:
            return False
        allowed = {word.casefold() for word in self.allow_list}
        words = re.findall(r"[\wÄÖÜäöüß.-]+", value)
        return bool(words) and all(w.casefold().strip(".") in allowed for w in words)

    def mask(self, text: str) -> MaskedText:
        return MaskingScope(self).mask(text)


# --- scopes -----------------------------------------------------------------------------

_GLUED = re.compile(r"[\w-]")
_WORD = re.compile(r"\w+")


class MaskingScope:
    """The placeholders of one unit of work of one owner.

    Every text masked inside the scope shares one numbering, and every value detected
    (or primed) in it is masked wherever it recurs in a later text of the same scope,
    also in another case or with a genitive "s" ("Brenneckes Haftung"). A model's answer,
    traced on its own, is therefore masked from the map of the very request that produced
    it. `restore_from` limits which values `core.llm` may write back into an answer.
    The scope lives as long as its `with` block; nothing outlives it.
    """

    def __init__(
        self,
        masker: Any,
        *,
        owner_id: str | None = None,
        subject_id: str | None = None,
        restore_from: Iterable[str] | None = None,
    ) -> None:
        self.masker = masker
        self.owner_id = owner_id
        self.subject_id = subject_id
        self._restore_texts = [text for text in (restore_from or ()) if text]
        self._restore_all = restore_from is None
        self._by_value: dict[tuple[str, str], str] = {}
        self.placeholders: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        # Values indexed by their first word (casefolded) for the recurrence search.
        self._index: dict[str, set[tuple[str, int, EntityKind]]] = {}
        self._lock = threading.Lock()
        self.failed: Exception | None = None

    def prime(self, *texts: str | None) -> None:
        """Detect in `texts` (the clause, its heading) without sending anything, so the
        scope knows their values before any request or trace."""
        for text in texts:
            if text:
                self.mask(text)

    def mask(self, text: str) -> MaskedText:
        if self.failed is not None:
            raise MaskingError(f"masking scope unusable: {self.failed}") from self.failed
        with self._lock:
            spans = self.masker.detect(text) if hasattr(self.masker, "detect") else []
            protected = _protected(text)
            repeats = [
                span
                for span in self._recurrences(text, spans)
                if not any(span.start < end and start < span.end for start, end in protected)
            ]
            spans = _resolve([*spans, *repeats])
            for match in PLACEHOLDER.finditer(text):
                kind, number = match.group(1), int(match.group(2))
                self._counters[kind] = max(self._counters.get(kind, 0), number)
            parts: list[str] = []
            cursor = 0
            for span in spans:
                value = text[span.start : span.end]
                token = self._token(span.kind, value)
                parts.append(text[cursor : span.start])
                parts.append(token)
                cursor = span.end
            parts.append(text[cursor:])
            return MaskedText("".join(parts), dict(self.placeholders), self._restorable())

    def mask_as(self, value: str, kind: EntityKind = "PERSON") -> MaskedText:
        """`value` as a whole is personal data of `kind` (a structured field such as a
        reviewer's name): one placeholder, without asking the detectors."""
        if not value.strip():
            return MaskedText(value)
        with self._lock:
            token = self._token(kind, value)
            return MaskedText(token, dict(self.placeholders), self._restorable())

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value)
        token = self._by_value.get(key)
        if token is None:
            number = self._counters.get(kind, 0) + 1
            while placeholder(kind, number) in self.placeholders:  # type: ignore[arg-type]
                number += 1
            self._counters[kind] = number
            token = placeholder(kind, number)  # type: ignore[arg-type]
            self._by_value[key] = token
            self.placeholders[token] = value
            first = _WORD.search(value)
            if first is not None:
                self._index.setdefault(first.group(0).casefold(), set()).add(
                    (value, first.start(), kind)  # type: ignore[arg-type]
                )
            if kind == "PERSON":
                surname = value.split()[-1] if value.split() else ""
                if len(surname) >= 3 and surname not in _NOT_A_NAME and surname != value:
                    self._index.setdefault(surname.casefold(), set()).add((surname, 0, "PERSON"))
                # The first name alone too: a cut excerpt ("… Ansprechpartnerin: Gundula").
                first_name = value.split()[0] if value.split() else ""
                if len(first_name) >= 3 and first_name not in _NOT_A_NAME and first_name != value:
                    self._index.setdefault(first_name.casefold(), set()).add(
                        (first_name, 0, "PERSON")
                    )
        return token

    def _recurrences(self, text: str, found: list[_Span]) -> list[_Span]:
        """Every place `text` repeats a value known to the scope or found in `text`,
        compared without case and allowing a genitive "s" after it."""
        local: dict[str, set[tuple[str, int, EntityKind]]] = {}
        for span in found:
            value = text[span.start : span.end]
            first = _WORD.search(value)
            if first is not None:
                local.setdefault(first.group(0).casefold(), set()).add(
                    (value, first.start(), span.kind)
                )
            if span.kind == "PERSON" and len(value.split()) > 1:
                surname = value.split()[-1]
                if len(surname) >= 3 and surname not in _NOT_A_NAME:
                    local.setdefault(surname.casefold(), set()).add((surname, 0, "PERSON"))
        if not local and not self._index:
            return []
        folded = text.casefold()
        same_length = len(folded) == len(text)  # casefold can change length ("ß" → "ss")
        extra: list[_Span] = []
        for word in _WORD.finditer(text):
            key = word.group(0).casefold()
            keys = (key, key[:-1]) if key.endswith("s") and len(key) > 3 else (key,)
            entries = {
                entry
                for name in keys
                for entry in (*self._index.get(name, ()), *local.get(name, ()))
            }
            for value, offset, kind in entries:
                start = word.start() - offset
                end = start + len(value)
                if start < 0 or end > len(text):
                    continue
                piece = text[start:end]
                if piece != value and not (same_length and folded[start:end] == value.casefold()):
                    continue
                if start > 0 and _GLUED.match(text[start - 1]):
                    continue
                tail = text[end : end + 2]
                if tail[:1].casefold() == "s" and not _GLUED.match(tail[1:2] or " "):
                    pass  # genitive: "Brenneckes"; the "s" stays outside the placeholder
                elif _GLUED.match(tail[:1] or " "):
                    continue
                extra.append(_Span(start, end, kind))
        return extra

    def _restorable(self) -> frozenset[str] | None:
        if self._restore_all:
            return None
        return frozenset(
            token
            for token, value in self.placeholders.items()
            if any(value in text for text in self._restore_texts)
        )


_SCOPE: contextvars.ContextVar[MaskingScope | None] = contextvars.ContextVar(
    "masking_scope", default=None
)


def current_scope() -> MaskingScope | None:
    return _SCOPE.get()


@contextmanager
def scope(
    owner_id: str | None,
    subject_id: str | None,
    *,
    prime: Iterable[str | None] = (),
    restore_from: Iterable[str] | None = None,
) -> Iterator[MaskingScope | None]:
    """Mask everything in this block (requests and traces) from one shared map, for one
    owner's unit of work (a clause, a summary, a feedback comment). `prime` the scope with
    the texts the work is about; `restore_from` limits which values an answer may get back
    (the clause text, for anything cached across owners). Masking off: a no-op."""
    masker = boundary().masker
    if masker is None:
        yield None
        return
    current = MaskingScope(
        masker, owner_id=owner_id, subject_id=subject_id, restore_from=restore_from
    )
    token = _SCOPE.set(current)
    try:
        try:
            current.prime(*prime)
        except Exception as exc:  # noqa: BLE001 - priming is detection; a fault blocks sends
            # Not raised here: every mask in this scope now fails, so each call fails the
            # way the caller already handles (a failed result), and nothing is sent.
            current.failed = exc
        yield current
    finally:
        _SCOPE.reset(token)


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

        Masks in the current scope (or a fresh one). Raises MaskingError if the masker
        fails, whatever it raised; callers turn that into a failed result without sending
        anything. Text that is already a `MaskedText` is not masked twice.
        """
        masker = self.masker
        if masker is None or isinstance(text, MaskedText):
            return text
        try:
            masked = self._mask(masker, text)
        except Exception as exc:  # noqa: BLE001 - any masker fault blocks the send
            raise MaskingError(f"masking failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(masked, MaskedText):
            raise MaskingError("masker returned an unmarked string")
        return masked

    @staticmethod
    def _mask(masker: Any, text: str) -> Any:
        if hasattr(masker, "detect"):
            active = current_scope()
            if active is None or active.masker is not masker:
                active = MaskingScope(masker)
            return active.mask(text)
        return masker.mask(text)

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
        value inside it masked, recursively through dicts, lists, pydantic models and
        dataclasses; anything else is masked as its `str`. Dict keys are ours (field
        names) and stay. Masking off: `data` unchanged.

        Inside a scope the scope's map masks the payload, so a model's answer is masked
        with the placeholders of the request that produced it; outside one, the payload
        gets one fresh scope of its own (shared numbering across its strings). A masker
        failure raises MaskingError; Langfuse then records its own fallback ("fully
        masked") instead of the data, so a failure never leaks.
        """
        masker = self.masker
        if masker is None:
            return data
        if current_scope() is None and hasattr(masker, "detect"):
            with scope(None, None):
                return self._walk(data)
        return self._walk(data)

    def _walk(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, MaskedText):
            return str(value)
        if isinstance(value, str):
            return str(self.outbound(value))
        if isinstance(value, Mapping):
            return {key: self._walk(item) for key, item in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._walk(item) for item in value]
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return self._walk(dump(mode="json"))
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self._walk(dataclasses.asdict(value))
        return self._walk(str(value))

    def trace_text(self, text: str | None, *, context: Iterable[str | None] = ()) -> str | None:
        """`text` masked for a Langfuse call the `mask=` hook does not cover (a score
        comment), in a scope primed with `context` (the clause it is about), so names the
        clause introduced are masked in the comment too. A masker failure drops the text
        (None) rather than sending it."""
        if text is None or not self.enabled:
            return text
        try:
            with scope(None, None, prime=context):
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
        allow_regex_only=bool(block.get("allow_regex_only", False)),
    )


# --- streaming: unmask a model's answer piece by piece -----------------------------------

_PARTIAL = re.compile(r"<[A-Z_]{0,16}\d{0,6}$")
_PREFIXES = tuple(f"<{kind}_" for kind in ENTITY_KINDS)


class StreamUnmasker:
    """Restores placeholders in a streamed answer (`MaskedText.display`). A placeholder
    can be split across two pieces ("<PERS" + "ON_1> sagt"), so a tail that could still
    become one is held back until the next piece (or `flush`) decides."""

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
        return self._masked.display(buffer)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._masked.display(rest)

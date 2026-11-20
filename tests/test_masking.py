"""9.A masking: detection, placeholders, the way back, statutes, every outbound hook.

All offline. The German spaCy model is not installed in CI, so the masker runs on its
regexes; one test feeds Presidio a blank spaCy pipeline with an entity ruler to prove the
NER layer is wired without downloading a model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from core import embeddings, langfuse_client, llm, masking, routing
from core.masking import (
    MaskedText,
    MaskerUnavailable,
    MaskingError,
    PresidioMasker,
    StreamUnmasker,
    UnmaskedPayload,
    boundary,
    masker_from_config,
)
from pydantic import BaseModel

FIXTURES = Path(__file__).parent / "fixtures" / "pii"
CORPUS = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))["clauses"]
STATUTES = json.loads((FIXTURES / "statutes.json").read_text(encoding="utf-8"))
BGB_CHUNKS = Path(__file__).parents[1] / "data" / "processed" / "bgb_chunks.jsonl"
GEMINI = {"provider": "gemini", "name": "fake", "max_output_tokens": 100}


@pytest.fixture(autouse=True)
def _masking_off():
    masking.configure(None)
    yield
    masking.configure(None)


@pytest.fixture
def masker() -> PresidioMasker:
    return PresidioMasker(allow_regex_only=True, allow_list=("BGB", "AGB", "Verwender", "Kunde"))


# --- detection --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "kind"),
    [
        ("Geschäftsführer: Max Mustermann", "Max Mustermann", "PERSON"),
        ("vertreten durch Frau Dr. Weber", "Weber", "PERSON"),
        ("die Haftung von Jürgen Schmitz ist begrenzt", "Jürgen Schmitz", "PERSON"),
        ("geb. Hartmann", "Hartmann", "PERSON"),
        ("Kontoinhaber: Fatma Yıldırım-Kranz", "Fatma Yıldırım-Kranz", "PERSON"),
        ("Hauptstraße 12a, 10115 Berlin", "Hauptstraße 12a, 10115 Berlin", "ADDRESS"),
        ("Am Mühlbach 7", "Am Mühlbach 7", "ADDRESS"),
        ("Postfach 61 02 33", "Postfach 61 02 33", "ADDRESS"),
        ("wohnhaft in 04109 Leipzig", "04109 Leipzig", "ADDRESS"),
        ("an anna.mueller@example.de schreiben", "anna.mueller@example.de", "EMAIL"),
        ("Tel.: +49 (0)30 123 45 67", "+49 (0)30 123 45 67", "PHONE"),
        ("unter 0171 2345678 erreichbar", "0171 2345678", "PHONE"),
        ("Fax 030 / 987 654-33", "030 / 987 654-33", "PHONE"),
        ("IBAN DE89 3704 0044 0532 0130 00", "DE89 3704 0044 0532 0130 00", "IBAN"),
        ("auf DE89370400440532013000 zahlen", "DE89370400440532013000", "IBAN"),
        ("USt-IdNr.: DE287654321", "DE287654321", "TAX_ID"),
        ("Steuer-ID 47 036 892 815", "47 036 892 815", "TAX_ID"),
        ("Steuernummer 06/123/45678", "06/123/45678", "TAX_ID"),
        ("Amtsgericht München HRB 123456", "HRB 123456", "REGISTER_NUMBER"),
        ("eingetragen unter HRA 702144 beim", "HRA 702144", "REGISTER_NUMBER"),
        ("geboren am 14.07.1961", "14.07.1961", "DATE_OF_BIRTH"),
        ("Geburtsdatum: 3. Mai 1985", "3. Mai 1985", "DATE_OF_BIRTH"),
        ("siehe https://www.linkedin.com/in/max-m-1", "https://www.linkedin.com/in/max-m-1", "URL"),
        ("unter https://shop.example/~mmueller.", "https://shop.example/~mmueller", "URL"),
    ],
)
def test_each_kind_is_found_and_replaced(masker, text, value, kind):
    masked = masker.mask(text)
    assert value not in masked
    assert masked.placeholders[f"<{kind}_1>"] == value
    assert masked.unmask() == text


@pytest.mark.parametrize(
    "text",
    [
        "Die Haftung ist auf 10000 Euro begrenzt; Mängel sind binnen 14 Tagen anzuzeigen.",
        "Der Vertrag beginnt am 01.03.2026 und läuft 24 Monate.",
        "Gerichtsstand ist Berlin. Es gilt das Recht der Bundesrepublik Deutschland.",
        "Weitere Informationen finden Sie unter https://www.shop.example/agb und /datenschutz.",
        "Die Lieferung erfolgt an die vom Kunden angegebene Adresse.",
        "Artikelnummer 0123456, Menge 3, Preis 19,99 EUR inkl. MwSt.",
        "Diese AGB gelten für alle Verträge der Musterhandel GmbH mit dem Kunden.",
    ],
)
def test_legal_prose_without_personal_data_is_untouched(masker, text):
    masked = masker.mask(text)
    assert masked == text and masked.placeholders == {}


def test_the_same_value_twice_is_one_placeholder_and_numbers_follow_first_appearance(masker):
    text = (
        "Geschäftsführer: Anna Kowalczyk, Max Feldhaus. Anna Kowalczyk haftet; Feldhaus nicht. "
        "Kontakt: a.k@example.de oder a.k@example.de."
    )
    masked = masker.mask(text)
    assert masked.count("<PERSON_1>") == 2 and masked.count("<EMAIL_1>") == 2
    assert masked.placeholders["<PERSON_1>"] == "Anna Kowalczyk"
    assert masked.placeholders["<PERSON_2>"] == "Max Feldhaus"
    assert masked.placeholders["<PERSON_3>"] == "Feldhaus"  # the surname alone, later on
    assert masked.unmask() == text
    assert masker.mask(text) == masked  # deterministic


@pytest.mark.parametrize("clause", CORPUS, ids=[clause["id"] for clause in CORPUS])
def test_the_corpus_round_trips_and_hides_every_seeded_value(masker, clause):
    masked = masker.mask(clause["text"])
    assert [value for value in clause["pii"] if value in masked] == []
    assert masked.unmask() == clause["text"]
    # A model's answer quoting every placeholder is restored for display.
    answer = " / ".join(masked.placeholders)
    assert masked.unmask(answer) == " / ".join(masked.placeholders.values())


def test_placeholders_already_in_the_text_keep_their_meaning(masker):
    text = "Antwort an <PERSON_1>; Ansprechpartnerin ist Frau Weber."
    masked = masker.mask(text)
    assert "<PERSON_1>" in masked and masked.placeholders == {"<PERSON_2>": "Weber"}
    assert masked.unmask() == text


def test_the_allow_list_and_the_entity_list_are_respected():
    masker = PresidioMasker(allow_regex_only=True, allow_list=("Kunde",), entities=("EMAIL",))
    text = "Herr Kunde zahlt an Frau Weber, weber@example.de."
    masked = masker.mask(text)
    assert masked == "Herr Kunde zahlt an Frau Weber, <EMAIL_1>."


def test_a_scope_masks_later_texts_from_its_own_map_and_nothing_outlives_it():
    """A model's answer, traced on its own, has no "Frau" in front of the name: the scope
    of the request that produced it knows the name. After the scope, nothing is known."""
    masking.configure(PresidioMasker(allow_regex_only=True))
    with masking.scope("owner-a", "review-1", prime=["Bürge ist Frau Gesine Quarck."]):
        answer = boundary().outbound("Gesine Quarck haftet; Quarcks Bürgschaft.")
        assert answer == "<PERSON_1> haftet; <PERSON_2>s Bürgschaft."
        traced = boundary().langfuse_mask(data={"output": "GESINE QUARCK haftet."})
        assert "GESINE" not in traced["output"]
    assert boundary().outbound("Gesine Quarck haftet.") == "Gesine Quarck haftet."


def test_masking_is_deterministic_and_never_crosses_owners():
    """Finding 3: owner A's imprint must not change owner B's clause (or its cached
    verdict). Masking a text gives the same result whatever was masked before."""
    masker = PresidioMasker(allow_regex_only=True)
    clause = "Ist der Kunde Kaufmann, so ist Gerichtsstand Hamburg (§ 38 ZPO)."
    masking.configure(masker)
    before = masker.mask(clause)
    with masking.scope("owner-a", "review-a", prime=["Ansprechpartnerin: Frau Kaufmann"]):
        pass
    masker.mask("Ansprechpartnerin ist Frau Sommer, Vertreter Herr Lange.")
    with masking.scope("owner-b", "review-b", prime=[clause]):
        sent = boundary().outbound(clause)
    assert before == clause == sent == masker.mask(clause)


# --- statutes ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "citation",
    [
        "§ 309 Nr. 7 BGB",
        "§ 309 Nr. 7 lit. a BGB",
        "§§ 305 ff. BGB",
        "§ 307 Abs. 1 Satz 2 BGB",
        "Art. 28 Abs. 3 lit. a DSGVO",
        "Art. 246a § 1 Abs. 2 EGBGB",
        "§ 38 ZPO",
        "BGBl. I S. 2909",
    ],
)
def test_statute_citations_are_never_touched(masker, citation):
    text = f"Die Klausel verstößt gegen {citation}; vgl. auch {citation}."
    assert masker.mask(text) == text


@pytest.mark.parametrize("chunk", STATUTES, ids=[chunk["chunk_id"] for chunk in STATUTES])
def test_statute_texts_pass_unchanged(masker, chunk):
    for key in ("text", "embed_text", "citation", "title"):
        masked = masker.mask(chunk[key])
        assert masked == chunk[key] and masked.placeholders == {}


@pytest.mark.skipif(not BGB_CHUNKS.exists(), reason="the processed BGB chunks are not built")
def test_the_whole_statute_index_passes_unchanged(masker):
    for line in BGB_CHUNKS.read_text(encoding="utf-8").splitlines():
        chunk = json.loads(line)
        assert masker.mask(chunk["text"]).placeholders == {}, chunk["chunk_id"]


# --- speed ------------------------------------------------------------------------------


def test_a_36_clause_document_masks_well_under_two_seconds():
    masker = PresidioMasker(allow_regex_only=True)
    clauses = [clause["text"] for clause in CORPUS] + [chunk["text"] for chunk in STATUTES]
    document = (clauses * 4)[:36]
    masker.mask("warm-up")
    started = time.perf_counter()
    for text in document:
        masker.mask(text)
    assert time.perf_counter() - started < 2.0


# --- NER layer, model and start-up ------------------------------------------------------


def test_a_missing_spacy_model_refuses_unless_regex_only_is_allowed(caplog):
    strict = PresidioMasker(spacy_model="de_core_news_not_installed")
    with pytest.raises(MaskerUnavailable):
        strict.load()  # build_app calls this: the API does not start
    masking.configure(strict)
    with pytest.raises(MaskingError):
        boundary().outbound("Frau Weber zahlt.")  # and a call would send nothing

    lenient = PresidioMasker(spacy_model="de_core_news_not_installed", allow_regex_only=True)
    assert lenient.mask("Frau Weber zahlt.") == "Frau <PERSON_1> zahlt."
    assert lenient.ner_available is False
    assert "regexes only" in caplog.text


def test_the_config_defaults_to_refusing_regex_only():
    def masker(**block):
        return masker_from_config({"privacy": {"masking": {"enabled": True, **block}}})

    assert masker().allow_regex_only is False
    assert masker(allow_regex_only=True).allow_regex_only is True


def test_presidio_ner_finds_a_name_without_any_context():
    spacy = pytest.importorskip("spacy")
    pytest.importorskip("presidio_analyzer")
    nlp = spacy.blank("de")
    ruler = nlp.add_pipe("entity_ruler")
    ruler.add_patterns([{"label": "PER", "pattern": "Gundula Quast"}])
    masker = PresidioMasker(nlp=nlp, score_threshold=0.1)  # NER present: no flag needed
    assert masker.ner_available
    masked = masker.mask("Die Ware holt Gundula Quast ab, siehe § 309 Nr. 7 BGB.")
    assert masked == "Die Ware holt <PERSON_1> ab, siehe § 309 Nr. 7 BGB."


# --- the Langfuse hook ------------------------------------------------------------------


class Payload(BaseModel):
    text: str


@dataclass
class Record:
    note: str


def test_the_langfuse_hook_masks_every_string_at_any_depth():
    data = {
        "input": [{"clause": "Ansprechpartnerin ist Frau Weber, weber@example.de."}],
        "output": Payload(text="Rückruf unter 0171 2345678"),
        "metadata": {"note": Record("IBAN DE89 3704 0044 0532 0130 00"), "tokens": 12},
        "reason_de": "Kein Personenbezug.",
    }
    assert boundary().langfuse_mask(data=data) is data  # off: unchanged

    masking.configure(PresidioMasker(allow_regex_only=True))
    sent = json.dumps(boundary().langfuse_mask(data=data), ensure_ascii=False)
    for value in ("Weber", "weber@example.de", "0171 2345678", "DE89 3704"):
        assert value not in sent
    assert '"tokens": 12' in sent
    # Keys are our own field names and stay; values get one numbering per payload.
    assert set(json.loads(sent)) == set(data)


def test_a_failing_masker_never_lets_trace_data_through():
    class Broken:
        def mask(self, text):
            raise RuntimeError("model gone")

    masking.configure(Broken())
    with pytest.raises(MaskingError):
        boundary().langfuse_mask(data={"input": "Frau Weber"})
    assert boundary().trace_text("Frau Weber") is None  # a score comment is dropped


def test_the_langfuse_client_installs_the_hook(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        langfuse_client,
        "load_settings",
        lambda: SimpleNamespace(
            langfuse_public_key="pk", langfuse_secret_key="sk", langfuse_host="h"
        ),
    )
    monkeypatch.setattr(langfuse_client, "Langfuse", lambda **kwargs: captured.update(kwargs))
    langfuse_client.client()
    assert captured["mask"] == boundary().langfuse_mask


def test_feedback_comments_and_dataset_items_are_masked():
    from app.domain import ClauseSpan, ClauseVerdict, Decision, Document, Review
    from app.review.corrections import Corrections, CorrectionSettings

    masking.configure(PresidioMasker(allow_regex_only=True))
    recorded = []
    langfuse = SimpleNamespace(
        create_dataset=lambda **kwargs: None,
        create_dataset_item=lambda **kwargs: recorded.append(kwargs),
    )
    text = "Haftung von Frau Weber (weber@example.de) ist ausgeschlossen."
    clause = ClauseSpan(position=0, number="1", heading="Haftung", text=text, content_hash="h")
    Corrections(
        langfuse, CorrectionSettings(enabled=True, allowed_sources=frozenset({"pdf"}))
    ).upsert(
        Document(
            document_id="d",
            filename="a.pdf",
            source_type="pdf",
            page_count=1,
            parser_version="p",
            splitter_version="s",
            text_hash="t",
        ),
        Review(review_id="r", document_id="d", agent_version="v", scope=[], status="finished"),
        clause,
        ClauseVerdict(
            content_hash="h", agent_version="v", verdict="potentially_void", confidence=0.9
        ),
        Decision(
            review_id="r",
            position=0,
            action="override",
            corrected_verdict="valid",
            model_verdict="potentially_void",
        ),
    )
    sent = json.dumps(recorded, ensure_ascii=False, default=str)
    assert recorded and "Weber" not in sent and "weber@example.de" not in sent
    assert "<PERSON_1>" in sent


# --- core.llm ---------------------------------------------------------------------------


class Strict:
    """A Gemini-shaped client that refuses unmasked text and records what it got."""

    def __init__(self, answer: str = "{}", pieces: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self.answer = answer
        self.pieces = pieces or []
        self.decisions = SimpleNamespace(create=self._decide)
        self.models = SimpleNamespace(
            generate_content=self._generate, generate_content_stream=self._stream
        )

    def _take(self, value):
        boundary().require_masked(value)
        self.sent.append(str(value))

    def _decide(self, *, model, input, questions):
        self._take(input)
        return SimpleNamespace(answers={}, usage=None)

    def _generate(self, *, model, contents, config):
        self._take(contents)
        return SimpleNamespace(text=self.answer, usage_metadata=None)

    def _stream(self, *, model, contents, config):
        self._take(contents)
        for piece in self.pieces:
            yield SimpleNamespace(text=piece, usage_metadata=None)
        yield SimpleNamespace(
            text="", usage_metadata=SimpleNamespace(prompt_token_count=3, candidates_token_count=2)
        )


CLAUSE = "Inhaberin ist Frau Weber, Hauptstraße 5\n10115 Berlin."


def test_decide_sends_only_masked_text_and_a_masker_failure_sends_nothing():
    client = Strict()
    masking.configure(PresidioMasker(allow_regex_only=True))
    assert llm.decide(CLAUSE, {}, GEMINI, client=client).error is None
    assert "Weber" not in client.sent[0] and "<PERSON_1>" in client.sent[0]

    class Broken:
        def mask(self, text):
            raise RuntimeError("model gone")

    masking.configure(Broken())
    result = llm.decide(CLAUSE, {}, GEMINI, client=client)
    assert result.error and "MaskingError" in result.error and len(client.sent) == 1


def test_complete_restores_placeholders_inside_the_json_answer():
    client = Strict(answer='{"reason_de": "<PERSON_1> wohnt in <ADDRESS_1>."}')
    masking.configure(PresidioMasker(allow_regex_only=True))
    result = llm.complete("System", CLAUSE, {}, GEMINI, client=client)
    assert "Weber" not in client.sent[0]
    # The address holds a line break: restored inside the parsed JSON, it stays valid.
    assert json.loads(result.text) == {"reason_de": "Weber wohnt in Hauptstraße 5\n10115 Berlin."}


def test_complete_stream_restores_a_placeholder_split_across_pieces():
    client = Strict(pieces=["Frau <PER", "SON_1> und <", "ADDRESS_1", ">!"])
    masking.configure(PresidioMasker(allow_regex_only=True))
    pieces = list(llm.complete_stream("System", CLAUSE, GEMINI, client=client))
    assert "".join(p.text for p in pieces) == "Frau Weber und Hauptstraße 5\n10115 Berlin!"
    assert pieces[-1].input_tokens == 3
    assert "Weber" not in client.sent[0]


def test_complete_stream_keeps_its_cancel_hook_and_fails_closed():
    client = Strict(pieces=["Frau <PERSON_1>"])
    masking.configure(PresidioMasker(allow_regex_only=True))
    stream = llm.complete_stream("System", CLAUSE, GEMINI, client=client)
    assert callable(stream.cancel) and callable(stream.close)
    stream.cancel()
    assert list(stream) == []

    class Broken:
        def mask(self, text):
            raise RuntimeError("model gone")

    masking.configure(Broken())
    sent_before = len(client.sent)
    (only,) = list(llm.complete_stream("System", CLAUSE, GEMINI, client=client))
    assert only.error and "MaskingError" in only.error and len(client.sent) == sent_before


def test_the_stream_unmasker_holds_back_only_what_could_become_a_placeholder():
    masked = MaskedText("<PERSON_1>", {"<PERSON_1>": "Weber"})
    unmasker = StreamUnmasker(masked)
    assert unmasker.feed("a < b und <PE") == "a < b und "
    assert unmasker.feed("RSON_1> ok <x") == "Weber ok <x"
    assert unmasker.feed("<PERS") == ""
    assert unmasker.flush() == "<PERS"


def test_masking_off_changes_nothing_on_the_wire():
    client = Strict(answer='{"reason_de": "x"}')
    llm.complete("System", CLAUSE, {}, GEMINI, client=client)
    assert client.sent == [CLAUSE]


# --- Jina -------------------------------------------------------------------------------


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_embeddings_key_the_cache_on_the_original_and_send_the_masked_text(monkeypatch):
    sent = []

    def post(url, *, headers, json, timeout):
        boundary().require_masked(json["input"])
        sent.extend(json["input"])
        return Response({"data": [{"index": 0, "embedding": [1.0, 0.0]}], "usage": {}})

    class Cache:
        def __init__(self):
            self.keys = []

        def get_embedding(self, digest, model):
            return None

        def put_embedding(self, digest, model, vector):
            self.keys.append(digest)

    monkeypatch.setattr(embeddings.requests, "post", post)
    monkeypatch.setattr(embeddings, "load_settings", lambda: SimpleNamespace(jina_api_key="k"))
    masking.configure(PresidioMasker(allow_regex_only=True))
    cache = Cache()
    embeddings.embed_texts([CLAUSE], "m", 2, "retrieval.query", cache=cache)
    assert "Weber" not in sent[0] and "<PERSON_1>" in sent[0]
    from core.hashing import content_hash

    assert cache.keys == [content_hash(CLAUSE)]


def test_the_topic_router_sends_masked_text(monkeypatch):
    sent = []

    def decide(text, questions, model, **kwargs):
        masked = boundary().outbound(text)
        boundary().require_masked(masked)
        sent.append(masked)
        return llm.DecisionResult(
            answers={"topic": llm.Answer(value="liability", confidence=0.9)},
            input_tokens=10,
            output_tokens=0,
            model="fake",
        )

    monkeypatch.setattr(routing, "decide", decide)
    masking.configure(PresidioMasker(allow_regex_only=True))
    assert routing.classify_topics([CLAUSE]) == ["liability"]
    assert "Weber" not in sent[0]


def test_a_strict_client_would_refuse_a_path_that_skipped_the_boundary():
    masking.configure(PresidioMasker(allow_regex_only=True))
    with pytest.raises(UnmaskedPayload):
        boundary().require_masked(["schon maskiert?"])


# --- eval harness -----------------------------------------------------------------------


def test_the_eval_harness_masks_as_the_app_config_says():
    from evals import harness

    with pytest.raises(MaskerUnavailable):  # no model: the run stops before any clause
        harness.configure_masking({"privacy": {"masking": {"enabled": True}}})
    harness.configure_masking({"privacy": {"masking": {"enabled": True, "allow_regex_only": True}}})
    assert isinstance(boundary().masker, PresidioMasker)
    custom = PresidioMasker(allow_regex_only=True, allow_list=("X",))
    masking.configure(custom)
    harness.configure_masking({"privacy": {"masking": {"enabled": False}}})
    assert boundary().masker is custom  # a configured boundary is left alone


def test_feedback_and_dismissal_score_comments_are_masked():
    from app.domain import Decision
    from tests.test_decisions import seeded_service

    masking.configure(PresidioMasker(allow_regex_only=True))
    _store, _service, feedback, langfuse = seeded_service()
    request = SimpleNamespace(
        review_id="review-1",
        position=0,
        vote="up",
        reasons=[],
        corrected_verdict=None,
        corrected_basis=[],
        comment="Mit Frau Weber (0171 2345678) besprochen.",
    )
    feedback.submit(request, author="ra")
    feedback.record_decision(
        Decision(
            review_id="review-1",
            position=0,
            action="dismiss",
            note="Laut Herrn Dr. Schmitz irrelevant.",
            model_verdict="potentially_void",
        )
    )
    comments = [score.get("comment") or "" for score in langfuse.scores.values()]
    assert len(comments) == 2  # the vote and the dismissal
    sent = " ".join(comments)
    assert "Weber" not in sent and "0171" not in sent and "Schmitz" not in sent
    assert "<PERSON_1>" in sent

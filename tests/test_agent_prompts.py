"""Prompt loading, fingerprinting, and the reason parser's refusal to raise."""

import json

import pytest

from app.agent import prompts


def setup_function() -> None:
    prompts.clear_cache()


def teardown_function() -> None:
    prompts.clear_cache()


def _no_langfuse(monkeypatch) -> None:
    """Make the Langfuse path unavailable, so `load` falls back to the repo file."""
    monkeypatch.setattr(
        prompts,
        "load_settings",
        lambda: type("S", (), {"langfuse_public_key": "", "langfuse_secret_key": ""})(),
    )


def test_every_named_prompt_exists_as_a_local_file(monkeypatch):
    _no_langfuse(monkeypatch)

    for name in prompts.PROMPT_NAMES.values():
        loaded = prompts.load(name)
        assert loaded.source == "local"
        assert loaded.text.strip(), f"{name} is empty"


def test_the_questions_mention_the_anonymisation_placeholders(monkeypatch):
    """Without this the model reads <<NAME>> as a drafting defect."""
    _no_langfuse(monkeypatch)

    void_text = prompts.load("void-question").text
    for placeholder in ("<<NAME>>", "<<STADT>>", "<<STRASSE>>"):
        assert placeholder in void_text


def test_the_basis_question_has_both_substitution_slots(monkeypatch):
    _no_langfuse(monkeypatch)

    template = prompts.load("basis-question").text
    rendered = template.format(citation="§ 309 Nr. 7 lit. a", text="Gesetzestext")

    assert "§ 309 Nr. 7 lit. a" in rendered
    assert "Gesetzestext" in rendered


def test_load_falls_back_to_the_file_when_langfuse_raises(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "load_settings",
        lambda: type("S", (), {"langfuse_public_key": "pk", "langfuse_secret_key": "sk"})(),
    )

    def explode(name, label):
        raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr(prompts, "_langfuse", explode)

    loaded = prompts.load("void-question")

    # A missing prompt server must not stop a clause being judged.
    assert loaded.source == "local"
    assert loaded.text.strip()


def test_langfuse_wins_when_it_answers(monkeypatch):
    monkeypatch.setattr(
        prompts,
        "load_settings",
        lambda: type("S", (), {"langfuse_public_key": "pk", "langfuse_secret_key": "sk"})(),
    )
    monkeypatch.setattr(
        prompts,
        "_langfuse",
        lambda name, label: prompts.PromptText(name, "remote text", "langfuse", version=7),
    )

    loaded = prompts.load("void-question")

    assert (loaded.source, loaded.version, loaded.text) == ("langfuse", 7, "remote text")


def test_fingerprint_covers_every_role_and_is_short_and_stable(monkeypatch):
    _no_langfuse(monkeypatch)

    first = prompts.fingerprint()
    second = prompts.fingerprint()

    assert set(first) == set(prompts.PROMPT_NAMES)
    assert first == second
    assert all(len(value) == 12 for value in first.values())


def test_fingerprint_moves_when_the_text_moves(monkeypatch):
    _no_langfuse(monkeypatch)
    before = prompts.fingerprint()["void"]

    prompts.clear_cache()
    monkeypatch.setattr(prompts, "_local", lambda name: "andere Frage")
    after = prompts.fingerprint()["void"]

    # Revised wording must change the agent version, or a stale verdict is
    # served as current.
    assert before != after


@pytest.mark.parametrize(
    ("key", "valid"),
    [
        ("bgb-309-7-a", True),
        ("bgb-475-abs2", True),
        ("is_void", True),
        ("bgb 309/7", False),
        ("", False),
        ("x" * 65, False),
    ],
)
def test_valid_question_key(key, valid):
    assert prompts.valid_question_key(key) is valid


def test_read_reason_reads_clean_json():
    assert prompts.read_reason('{"reason_de": "Unwirksam nach § 309."}') == "Unwirksam nach § 309."


def test_read_reason_recovers_json_wrapped_in_prose():
    text = 'Hier das Ergebnis:\n{"reason_de": "Unwirksam."}\nViele Grüße'

    assert prompts.read_reason(text) == "Unwirksam."


@pytest.mark.parametrize("text", ["", "Das kann ich nicht sagen.", "{nope}", "{}"])
def test_read_reason_never_raises(text):
    """A missing reason is cosmetic; the verdict is already decided."""
    assert prompts.read_reason(text) == ""


def test_read_reason_truncates_to_the_schema_cap():
    long = "ü" * (prompts.REASON_MAX_CHARS + 200)

    result = prompts.read_reason(json.dumps({"reason_de": long}))

    assert len(result) == prompts.REASON_MAX_CHARS


def test_reason_schema_caps_the_reason_and_is_closed():
    schema = prompts.reason_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["reason_de"]
    assert schema["properties"]["reason_de"]["maxLength"] == prompts.REASON_MAX_CHARS


def test_seed_pushes_every_prompt_with_the_label():
    pushed = []

    class FakeClient:
        def create_prompt(self, **kwargs):
            pushed.append(kwargs)

    prompts.seed(FakeClient(), label="evaluation")

    assert {p["name"] for p in pushed} == set(prompts.PROMPT_NAMES.values())
    assert all(p["labels"] == ["evaluation"] for p in pushed)
    assert all(p["type"] == "text" for p in pushed)

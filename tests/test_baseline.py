import pandas as pd

from evals.baseline import (
    SYSTEM_PROMPT,
    BaselineOutput,
    answer_schema,
    classify,
    parse_answer,
    prompt_fingerprint,
    render_user_prompt,
    task,
)
from evals.datasets import INPUT_KEYS, load_dev

CLAUSE = {
    "clause_id": "agb-349",
    "title": "6 Haftung",
    "text": "Das Recht auf Schadenersatz wird beschränkt auf Vorsatz oder grobe Fahrlässigkeit.",
    "topics": ["liability"],
}


class _GenUsage:
    def __init__(self, prompt=11, candidates=5):
        self.prompt_token_count, self.candidates_token_count = prompt, candidates


class _GenResponse:
    def __init__(self, text):
        self.text, self.usage_metadata = text, _GenUsage()


class _FakeModels:
    def __init__(self, text=None, raises=None):
        self.text, self.raises, self.calls = text, raises, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return _GenResponse(self.text)


class _FakeClient:
    """Gemini-shaped: the baseline now runs on whatever provider config names."""

    def __init__(self, text=None, raises=None):
        self.models = _FakeModels(text, raises)


# `name` rather than `model`, because that is the key core.llm reads.
MODEL = {"provider": "gemini", "name": "gemini-3.8-flash", "max_output_tokens": 400}


def test_prompt_mentions_the_anonymisation_placeholders():
    # Without this the model reads <<NAME>> as a drafting defect.
    for placeholder in ("<<NAME>>", "<<STADT>>", "<<STRASSE>>"):
        assert placeholder in SYSTEM_PROMPT


def test_render_user_prompt_survives_a_missing_title():
    rendered = render_user_prompt({**CLAUSE, "title": "", "topics": []})

    assert "(ohne Überschrift)" in rendered
    assert "(ohne Thema)" in rendered
    assert CLAUSE["text"] in rendered


def test_parse_answer_reads_clean_json():
    output = parse_answer('{"verdict": "potentially_void", "reason_de": "x", "confidence": 0.9}')

    assert output.verdict == "potentially_void"
    assert output.confidence == 0.9
    assert output.error is None


def test_parse_answer_recovers_json_wrapped_in_prose():
    output = parse_answer('Hier mein Ergebnis:\n{"verdict": "valid"}\nViele Grüße')

    assert output.verdict == "valid"
    assert output.error is None


def test_parse_answer_returns_a_null_verdict_on_garbage():
    output = parse_answer("Das kann ich nicht beurteilen.")

    # Null, not a guess: scored as "not void", so it costs recall.
    assert output.verdict is None
    assert "unparseable" in output.error


def test_parse_answer_rejects_an_invented_verdict():
    output = parse_answer('{"verdict": "probably_fine"}')

    assert output.verdict is None


def test_classify_turns_an_api_failure_into_data():
    client = _FakeClient(raises=RuntimeError("rate limited"))

    output = classify(CLAUSE, MODEL, client=client)

    assert isinstance(output, BaselineOutput)
    assert output.verdict is None
    assert "RuntimeError" in output.error


def test_classify_passes_the_model_the_system_prompt_and_the_schema():
    client = _FakeClient(text='{"verdict": "valid", "reason_de": "ok", "confidence": 0.8}')

    output = classify(CLAUSE, MODEL, client=client)

    call = client.models.calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert render_user_prompt(CLAUSE) in call["contents"]
    # The system prompt and the schema both travel in the config, and the
    # schema is enforced by the API rather than asked for in the prompt.
    config = call["config"]
    assert config["system_instruction"] == SYSTEM_PROMPT
    assert config["response_json_schema"] == answer_schema()
    # Sending both schema parameters is rejected, so only the JSON-Schema one
    # may be present.
    assert "response_schema" not in config
    assert output.verdict == "valid"
    # Token counts come back so a run can be costed.
    assert (output.input_tokens, output.output_tokens) == (11, 5)


def test_answer_schema_is_closed_and_fully_required():
    schema = answer_schema()

    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["confidence", "reason_de", "verdict"]
    # The three allowed verdicts come from core.schemas, not a second list.
    assert set(schema["properties"]["verdict"]["enum"]) == {
        "valid",
        "potentially_void",
        "uncertain",
    }


def test_classify_reads_the_providers_text():
    client = _FakeClient(text='{"verdict": "potentially_void"}')

    assert classify(CLAUSE, MODEL, client=client).verdict == "potentially_void"


def test_task_accepts_both_item_shapes():
    client = _FakeClient(text='{"verdict": "potentially_void", "reason_de": "r"}')
    run = task(MODEL, client=client)

    as_dict = run(item={"input": CLAUSE})
    as_object = run(item=type("Item", (), {"input": CLAUSE})())

    assert as_dict["verdict"] == "potentially_void"
    assert as_object["verdict"] == "potentially_void"


def test_prompt_fingerprint_is_stable_and_short():
    assert prompt_fingerprint() == prompt_fingerprint()
    assert len(prompt_fingerprint()) == 12


def test_dev_items_carry_exactly_the_published_input_keys(tmp_path):
    # Dev and held-out runs must hand the task identical shapes, or their
    # numbers are not comparable.
    frame = pd.DataFrame(
        [
            {
                "clause_id": "agb-349",
                "contract_id": 12,
                "section_no": "6",
                "section_title": "Haftung",
                "text": CLAUSE["text"],
                "topics": ["liability"],
                "is_fragment": False,
                "foreign_law_choice": False,
                "label_void": True,
            }
        ]
    )
    path = tmp_path / "train.parquet"
    frame.to_parquet(path, index=False)

    items = load_dev(path)

    assert len(items) == 1
    assert tuple(items[0].input) == INPUT_KEYS
    assert items[0].input["title"] == "6 Haftung"
    assert items[0].label_void is True
    assert "label_void" not in items[0].input
    assert items[0].as_local()["metadata"]["clause_id"] == "agb-349"

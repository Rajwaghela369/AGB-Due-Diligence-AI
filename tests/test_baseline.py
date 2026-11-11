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


class _TextBlock:
    """One content block of an Anthropic response."""

    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_TextBlock(text)]


class _FakeMessages:
    def __init__(self, text=None, raises=None):
        self.text, self.raises, self.calls = text, raises, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return _FakeResponse(self.text)


class _FakeClient:
    def __init__(self, text=None, raises=None):
        self.messages = _FakeMessages(text, raises)


MODEL = {"model": "claude-haiku-4-5-20251001", "max_output_tokens": 400}


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

    output = classify(CLAUSE, client, MODEL)

    assert isinstance(output, BaselineOutput)
    assert output.verdict is None
    assert "RuntimeError" in output.error


def test_classify_passes_the_model_the_system_prompt_and_the_schema():
    client = _FakeClient(text='{"verdict": "valid", "reason_de": "ok", "confidence": 0.8}')

    output = classify(CLAUSE, client, MODEL)

    call = client.messages.calls[0]
    assert call["model"] == "claude-haiku-4-5-20251001"
    # The system prompt is its own parameter, not a message.
    assert call["system"] == SYSTEM_PROMPT
    assert call["messages"] == [{"role": "user", "content": render_user_prompt(CLAUSE)}]
    # Structured output is enforced by the API, not asked for in the prompt.
    assert call["output_config"]["format"]["type"] == "json_schema"
    # temperature is not a parameter of this API version; passing it 500s.
    assert "temperature" not in call
    assert output.verdict == "valid"


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


def test_classify_reassembles_multiple_text_blocks():
    client = _FakeClient(text='{"verdict": "potentially_void"}')

    assert classify(CLAUSE, client, MODEL).verdict == "potentially_void"


def test_task_accepts_both_item_shapes():
    client = _FakeClient(text='{"verdict": "potentially_void", "reason_de": "r"}')
    run = task(client, MODEL)

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

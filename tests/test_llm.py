import pytest
from core.budget import BudgetExceeded, RequestBudget
from core.llm import ANTHROPIC, GEMINI, Answer, complete, decide

GEMINI_MODEL = {"provider": GEMINI, "name": "gemini-decision-1"}
GEMINI_GEN = {"provider": GEMINI, "name": "gemini-3.6-flash", "max_output_tokens": 500}
ANTHROPIC_GEN = {"provider": ANTHROPIC, "name": "claude-haiku-4-5-20251001"}

QUESTIONS = {
    "is_void": {"type": "noul", "instructions": "Is the clause void?"},
    "topic": {
        "type": "choice",
        "instructions": "Which topic?",
        "criteria": {"liability": "Haftung", "warranty": "Gewährleistung", "other": ""},
    },
}


# --- fakes shaped like the real SDK objects -------------------------------------


class _NoulAnswer:
    """Mirrors google.genai.decisions.NoulAnswer: confidence, noul, type."""

    def __init__(self, noul, confidence):
        self.type, self.noul, self.confidence = "noul", noul, confidence


class _ChoiceAnswer:
    """Mirrors ChoiceAnswer: choice, confidence, probabilities, type."""

    def __init__(self, choice, confidence, probabilities):
        self.type, self.choice = "choice", choice
        self.confidence, self.probabilities = confidence, probabilities


class _Usage:
    def __init__(self, in_tokens, out_tokens):
        self.total_input_tokens = in_tokens
        self.total_output_tokens = out_tokens


class _DecisionResponse:
    def __init__(self, answers, usage):
        self.answers, self.usage = answers, usage


class _FakeDecisions:
    def __init__(self, response=None, raises=None):
        self.response, self.raises, self.calls = response, raises, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.response


class _FakeGenaiClient:
    def __init__(self, response=None, raises=None, text="{}", usage=None):
        self.decisions = _FakeDecisions(response, raises)
        self.models = _FakeModels(text, usage)


class _GenUsage:
    def __init__(self, prompt, candidates):
        self.prompt_token_count, self.candidates_token_count = prompt, candidates


class _GenResponse:
    def __init__(self, text, usage):
        self.text, self.usage_metadata = text, usage


class _FakeModels:
    def __init__(self, text="{}", usage=None):
        self.text, self.usage, self.calls = text, usage, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return _GenResponse(self.text, self.usage or _GenUsage(10, 20))


class _TextBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class _AnthropicResponse:
    def __init__(self, text):
        self.content = [_TextBlock(text)]
        self.usage = type("U", (), {"input_tokens": 7, "output_tokens": 3})()


class _FakeMessages:
    def __init__(self, text):
        self.text, self.calls = text, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _AnthropicResponse(self.text)


class _FakeAnthropicClient:
    def __init__(self, text='{"ok": true}'):
        self.messages = _FakeMessages(text)


def _response():
    return _DecisionResponse(
        answers={
            "is_void": _NoulAnswer(0.93, 0.93),
            "topic": _ChoiceAnswer("liability", 0.81, {"liability": 0.81, "warranty": 0.19}),
        },
        usage=_Usage(324, 0),
    )


# --- decide() -------------------------------------------------------------------


def test_decide_maps_noul_and_choice_answers():
    client = _FakeGenaiClient(response=_response())

    result = decide("Die Haftung ist ausgeschlossen.", QUESTIONS, GEMINI_MODEL, client=client)

    assert result.error is None
    assert result.answers["is_void"] == Answer(value=0.93, confidence=0.93, probabilities=None)
    topic = result.answers["topic"]
    assert topic.value == "liability"
    assert topic.probabilities == {"liability": 0.81, "warranty": 0.19}


def test_decide_carries_token_counts_and_output_is_zero():
    client = _FakeGenaiClient(response=_response())

    result = decide("x", QUESTIONS, GEMINI_MODEL, client=client)

    # The Decisions API generates no text, so output tokens are always 0.
    assert (result.input_tokens, result.output_tokens) == (324, 0)
    assert result.model == "gemini-decision-1"


def test_decide_passes_model_input_and_questions_through_verbatim():
    client = _FakeGenaiClient(response=_response())

    decide("clause text", QUESTIONS, GEMINI_MODEL, client=client)

    call = client.decisions.calls[0]
    assert call == {
        "model": "gemini-decision-1",
        "input": "clause text",
        "questions": QUESTIONS,
    }


def test_decide_returns_an_error_result_instead_of_raising():
    client = _FakeGenaiClient(raises=RuntimeError("429 RESOURCE_EXHAUSTED"))

    result = decide("x", QUESTIONS, GEMINI_MODEL, client=client)

    assert result.answers == {}
    assert "RuntimeError" in result.error
    assert "RESOURCE_EXHAUSTED" in result.error


def test_decide_rejects_a_provider_it_cannot_serve():
    with pytest.raises(NotImplementedError, match="'anthropic'"):
        decide("x", QUESTIONS, {"provider": ANTHROPIC, "name": "claude"}, client=object())


def test_decide_reserves_budget_before_calling():
    client = _FakeGenaiClient(response=_response())
    budget = RequestBudget(max_requests=1)

    decide("x", QUESTIONS, GEMINI_MODEL, budget=budget, client=client)

    assert budget.spent() == 1


def test_budget_exhaustion_stops_the_run_rather_than_erroring_per_clause():
    # The $5 Anthropic run degraded instead of stopping, returning a
    # verdict-shaped error for 113 clauses. BudgetExceeded is our own control
    # signal, so it propagates where provider faults do not.
    client = _FakeGenaiClient(response=_response())
    budget = RequestBudget(max_requests=1)
    decide("x", QUESTIONS, GEMINI_MODEL, budget=budget, client=client)

    with pytest.raises(BudgetExceeded):
        decide("x", QUESTIONS, GEMINI_MODEL, budget=budget, client=client)

    assert len(client.decisions.calls) == 1


def test_budget_refusal_happens_before_the_request():
    budget = RequestBudget(max_requests=0)
    client = _FakeGenaiClient(response=_response())

    with pytest.raises(BudgetExceeded):
        decide("x", QUESTIONS, GEMINI_MODEL, budget=budget, client=client)

    assert client.decisions.calls == []


def test_a_provider_fault_is_still_data_not_a_crash():
    # The distinction that matters: provider errors are absorbed, budget stops
    # are not.
    client = _FakeGenaiClient(raises=RuntimeError("500 internal"))
    budget = RequestBudget(max_requests=10)

    result = decide("x", QUESTIONS, GEMINI_MODEL, budget=budget, client=client)

    assert result.error is not None
    assert budget.spent() == 1


def test_complete_propagates_budget_exhaustion_too():
    client = _FakeAnthropicClient()
    budget = RequestBudget(max_requests=0)

    with pytest.raises(BudgetExceeded):
        complete("s", "u", SCHEMA, ANTHROPIC_GEN, budget=budget, client=client)


# --- complete() -----------------------------------------------------------------


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"reason_de": {"type": "string"}},
    "required": ["reason_de"],
}


def test_complete_gemini_uses_response_json_schema_not_response_schema():
    client = _FakeGenaiClient(text='{"reason_de": "unwirksam"}')

    result = complete("sys", "user", SCHEMA, GEMINI_GEN, client=client)

    config = client.models.calls[0]["config"]
    # response_json_schema is the parameter that accepts a plain JSON Schema;
    # response_schema must be omitted alongside it.
    assert config["response_json_schema"] == SCHEMA
    assert "response_schema" not in config
    assert config["response_mime_type"] == "application/json"
    assert config["system_instruction"] == "sys"
    assert config["max_output_tokens"] == 500
    assert result.text == '{"reason_de": "unwirksam"}'
    assert (result.input_tokens, result.output_tokens) == (10, 20)


def test_complete_anthropic_sends_a_json_schema_output_config():
    client = _FakeAnthropicClient(text='{"reason_de": "ok"}')

    result = complete("sys", "user", SCHEMA, ANTHROPIC_GEN, client=client)

    call = client.messages.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] == SCHEMA
    assert call["system"] == "sys"
    # temperature is not a parameter of this API version.
    assert "temperature" not in call
    assert result.text == '{"reason_de": "ok"}'
    assert (result.input_tokens, result.output_tokens) == (7, 3)


def test_complete_returns_an_error_result_instead_of_raising():
    class _Boom:
        def __init__(self):
            self.messages = self

        def create(self, **_):
            raise TimeoutError("read timeout")

    result = complete("sys", "user", SCHEMA, ANTHROPIC_GEN, client=_Boom())

    assert result.text == ""
    assert "TimeoutError" in result.error


def test_complete_rejects_an_unknown_provider():
    with pytest.raises(NotImplementedError, match="'openai'"):
        complete("s", "u", SCHEMA, {"provider": "openai", "name": "gpt"}, client=object())

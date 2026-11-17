"""Provider-neutral model calls: typed decisions, and generated text.

Two shapes, because the agent needs two different things:

`decide` wraps the Gemini Decisions API, which answers closed questions with
calibrated probabilities and generates **zero output tokens**. One forward pass
answers every question, so asking "is this void?" and "which topic?" together
costs barely more than asking one. That is what makes a free-tier run of the
whole dev half affordable, and it replaces both the router LLM call and the
judge's verdict.

`complete` wraps ordinary generation, for the one thing decisions cannot give
us: the German reason a reviewer reads.

Neither function raises on a provider error. A failed call is returned as a
result carrying `error`, so one bad clause costs recall instead of killing the
run — the lesson of the Phase 3 baseline, and of the Phase 4 run that lost 116
clauses to an unhandled parse error.

`BudgetExceeded` is the one exception that does propagate. It is our own
control signal rather than a provider fault, and swallowing it would recreate
the failure this module exists to prevent: the $5 Anthropic run did not stop
when it ran out, it degraded, and returned a verdict-shaped error for every
remaining clause. A run out of allowance should stop.
"""

import os
import random
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from core.budget import BudgetExceeded, RequestBudget
from core.logging import get_logger

log = get_logger("core.llm")

GEMINI = "gemini"
ANTHROPIC = "anthropic"

# Decisions clamps yes/no probabilities into this range, so a threshold at or
# above the ceiling can never fire. Callers comparing against `value` need it.
NOUL_FLOOR = 0.01
NOUL_CEILING = 0.99
MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.5


@dataclass(frozen=True)
class Answer:
    """One decision answer. `value` is a probability for noul, an option id for choice."""

    value: Any
    confidence: float
    probabilities: dict[str, float] | None = None


@dataclass(frozen=True)
class DecisionResult:
    answers: dict[str, Answer]
    input_tokens: int
    output_tokens: int
    model: str
    error: str | None = None


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    error: str | None = None


def _failed_decision(model: str, exc: Exception) -> DecisionResult:
    log.warning("decision call failed: %s: %s", type(exc).__name__, exc)
    return DecisionResult({}, 0, 0, model, f"{type(exc).__name__}: {exc}")


def _failed_completion(model: str, exc: Exception) -> Completion:
    log.warning("completion call failed: %s: %s", type(exc).__name__, exc)
    return Completion("", 0, 0, model, f"{type(exc).__name__}: {exc}")


GEMINI_TIMEOUT_MS = int(os.environ.get("GEMINI_TIMEOUT_MS", "60000"))


def _gemini_client(client: Any = None) -> Any:
    if client is not None:
        return client

    from google import genai

    from core.settings import load_settings

    api_key = load_settings().gemini_api_key
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set; copy .env.example to .env")
    # The Decisions API lives on v1beta only. The timeout is per HTTP request, in
    # milliseconds: without one, a response that never comes blocked a worker (and the
    # clause's single-flight lock) indefinitely, as seen live on 2026-11-17. A timeout
    # surfaces as an error the retry and failure paths already handle.
    return genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1beta", "timeout": GEMINI_TIMEOUT_MS},
    )


def _anthropic_client(client: Any = None) -> Any:
    if client is not None:
        return client

    from anthropic import Anthropic

    from core.settings import load_settings

    api_key = load_settings().anthropic_api_key
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set; copy .env.example to .env")
    return Anthropic(api_key=api_key)


def _read_answer(raw: Any) -> Answer:
    """Map a Decisions answer onto our shape without caring which type it is.

    noul answers carry `noul`; choice answers carry `choice` and
    `probabilities`; score answers carry `score`. Reading whichever is present
    keeps this working if a question type is added later.
    """
    for attribute in ("noul", "choice", "score"):
        value = getattr(raw, attribute, None)
        if value is not None:
            break
    else:
        value = None

    probabilities = getattr(raw, "probabilities", None)
    return Answer(
        value=value,
        confidence=float(getattr(raw, "confidence", 0.0) or 0.0),
        probabilities=dict(probabilities) if probabilities else None,
    )


def _tokens(usage: Any) -> tuple[int, int]:
    return (
        int(getattr(usage, "total_input_tokens", 0) or 0),
        int(getattr(usage, "total_output_tokens", 0) or 0),
    )


def _status_code(exc: Exception) -> int | None:
    """Extract an HTTP status across the Google and Anthropic SDK exception shapes."""
    response = getattr(exc, "response", None)
    candidates = (
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(response, "status_code", None),
    )
    for candidate in candidates:
        candidate = getattr(candidate, "value", candidate)
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    match = re.search(r"(?<!\d)(429|5\d\d)(?!\d)", str(exc))
    return int(match.group(1)) if match else None


def _retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    return status == 429 or (status is not None and 500 <= status < 600)


def _request_with_retries[T](call: Callable[[], T], budget: RequestBudget | None) -> T:
    """Retry transient provider responses, accounting each network attempt separately."""
    for attempt in range(MAX_ATTEMPTS):
        if budget is not None:
            budget.reserve()
        try:
            return call()
        except BudgetExceeded:
            raise
        except Exception as exc:
            if attempt + 1 == MAX_ATTEMPTS or not _retryable(exc):
                raise
            delay = _BACKOFF_SECONDS * (2**attempt)
            sleep_for = random.uniform(delay / 2, delay)
            log.warning(
                "provider call returned %s; retrying attempt %s/%s in %.2fs",
                _status_code(exc),
                attempt + 2,
                MAX_ATTEMPTS,
                sleep_for,
            )
            time.sleep(sleep_for)
    raise AssertionError("retry loop exhausted")


def decide(
    text: str,
    questions: dict[str, dict],
    model: dict,
    budget: RequestBudget | None = None,
    client: Any = None,
) -> DecisionResult:
    """Answer typed questions about one text in a single request.

    `questions` is passed through verbatim in the Decisions API's own shape:
    {key: {"type": "noul"|"choice"|"score", "instructions": str, "criteria": ...}}.
    Keys come back as the keys of `answers`.
    """
    provider = model.get("provider")
    name = model.get("name", "")
    if provider != GEMINI:
        raise NotImplementedError(
            f"decide() supports only the {GEMINI!r} provider; configs ask for {provider!r}"
        )

    try:
        resolved = _gemini_client(client)
        result = _request_with_retries(
            lambda: resolved.decisions.create(model=name, input=text, questions=questions),
            budget,
        )
    except BudgetExceeded:
        # Not a provider fault: the run is out of allowance and must stop.
        raise
    except Exception as exc:  # noqa: BLE001 - a failed call is data, not a crash
        return _failed_decision(name, exc)

    answers = {key: _read_answer(raw) for key, raw in (result.answers or {}).items()}
    input_tokens, output_tokens = _tokens(getattr(result, "usage", None))
    return DecisionResult(answers, input_tokens, output_tokens, name)


def complete(
    system: str,
    user: str,
    schema: dict,
    model: dict,
    budget: RequestBudget | None = None,
    client: Any = None,
) -> Completion:
    """Generate JSON matching `schema`, from whichever provider the config names."""
    provider = model.get("provider")
    name = model.get("name", "")
    max_tokens = int(model.get("max_output_tokens", 800))

    try:
        if provider == GEMINI:
            return _complete_gemini(system, user, schema, name, max_tokens, budget, client)
        if provider == ANTHROPIC:
            return _complete_anthropic(system, user, schema, name, max_tokens, budget, client)
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed call is data, not a crash
        return _failed_completion(name, exc)

    raise NotImplementedError(f"complete() does not support provider {provider!r}")


def _complete_gemini(
    system: str,
    user: str,
    schema: dict,
    name: str,
    max_tokens: int,
    budget: RequestBudget | None,
    client: Any,
) -> Completion:
    resolved = _gemini_client(client)

    # `response_json_schema` is the parameter that accepts a plain JSON Schema;
    # `response_schema` wants a genai Schema or pydantic type and must be
    # omitted alongside it. `response_mime_type` is required either way.
    response = _request_with_retries(
        lambda: resolved.models.generate_content(
            model=name,
            contents=user,
            config={
                "system_instruction": system,
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json",
                "response_json_schema": schema,
            },
        ),
        budget,
    )
    usage = getattr(response, "usage_metadata", None)
    return Completion(
        text=getattr(response, "text", "") or "",
        input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
        model=name,
    )


def _complete_anthropic(
    system: str,
    user: str,
    schema: dict,
    name: str,
    max_tokens: int,
    budget: RequestBudget | None,
    client: Any,
) -> Completion:
    resolved = _anthropic_client(client)

    # Matches evals/baseline.py: the schema is enforced by the API, and
    # `temperature` is not a parameter of this API version.
    response = _request_with_retries(
        lambda: resolved.messages.create(
            model=name,
            max_tokens=max_tokens,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        ),
        budget,
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    usage = getattr(response, "usage", None)
    return Completion(
        text=text,
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        model=name,
    )


class _GeminiCompletionStream(Iterator[Completion]):
    """A cancellable provider iterator, so an SSE disconnect can abort its HTTP request."""

    def __init__(
        self,
        system: str,
        user: str,
        name: str,
        max_tokens: int,
        budget: RequestBudget | None,
        client: Any,
    ) -> None:
        self._system = system
        self._user = user
        self._name = name
        self._max_tokens = max_tokens
        self._budget = budget
        self._client = client
        self._provider: Iterator[Any] | None = None
        self._provider_closed = False
        self._started = False
        self._done = False
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._input_tokens = 0
        self._output_tokens = 0

    def __iter__(self) -> "_GeminiCompletionStream":
        return self

    def __next__(self) -> Completion:
        if self._done or self._cancelled.is_set():
            self._finish(cancel=True)
            raise StopIteration
        if not self._started:
            self._started = True
            try:
                resolved = _gemini_client(self._client)
                if self._budget is not None:
                    self._budget.reserve()
                provider = resolved.models.generate_content_stream(
                    model=self._name,
                    contents=self._user,
                    config={
                        "system_instruction": self._system,
                        "max_output_tokens": self._max_tokens,
                    },
                )
                with self._lock:
                    self._provider = iter(provider)
            except BudgetExceeded:
                self._done = True
                raise
            except Exception as exc:  # noqa: BLE001 - provider failure is result data
                return self._error(exc)
            if self._cancelled.is_set():
                self._finish(cancel=True)
                raise StopIteration

        while not self._cancelled.is_set():
            try:
                assert self._provider is not None
                response = next(self._provider)
            except StopIteration:
                if self._cancelled.is_set():
                    self._finish(cancel=True)
                    raise
                result = Completion("", self._input_tokens, self._output_tokens, self._name)
                self._finish()
                return result
            except Exception as exc:  # noqa: BLE001 - provider failure is result data
                if self._cancelled.is_set():
                    self._finish(cancel=True)
                    raise StopIteration from None
                return self._error(exc)
            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                self._input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
                self._output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            text = getattr(response, "text", "") or ""
            if text:
                return Completion(text, 0, 0, self._name)
        self._finish(cancel=True)
        raise StopIteration

    def _error(self, exc: Exception) -> Completion:
        log.warning("streamed completion failed: %s: %s", type(exc).__name__, exc)
        result = Completion(
            "",
            self._input_tokens,
            self._output_tokens,
            self._name,
            f"{type(exc).__name__}: {exc}",
        )
        self._finish(cancel=self._cancelled.is_set())
        return result

    def _finish(self, *, cancel: bool = False) -> None:
        self._done = True
        with self._lock:
            provider = self._provider
            if provider is None or self._provider_closed:
                return
            self._provider_closed = True
        if cancel:
            callback = getattr(provider, "cancel", None)
            if callable(callback):
                with suppress(Exception):
                    callback()
        callback = getattr(provider, "close", None)
        if callable(callback):
            with suppress(Exception):
                callback()

    def cancel(self) -> None:
        """Thread-safe best-effort cancellation of the active provider response."""
        self._cancelled.set()
        self._finish(cancel=True)

    close = cancel


def complete_stream(
    system: str,
    user: str,
    model: dict,
    budget: RequestBudget | None = None,
    client: Any = None,
) -> Iterator[Completion]:
    """Generate plain text and yield it piece by piece, for an answer a person watches arrive
    (the rewrite chat). Each yielded `Completion` carries the next piece in `text`; the last
    one carries the usage, or `error` when the provider failed, and ends the stream.

    Exactly one request, reserved once and never retried: once a piece has been shown, a
    retry would show a second, different answer, and each message is budgeted as one call.
    Like `complete`, a provider failure is data and only `BudgetExceeded` propagates.
    """
    provider = model.get("provider")
    name = model.get("name", "")
    max_tokens = int(model.get("max_output_tokens", 800))
    if provider != GEMINI:
        raise NotImplementedError(f"complete_stream() does not support provider {provider!r}")

    return _GeminiCompletionStream(system, user, name, max_tokens, budget, client)

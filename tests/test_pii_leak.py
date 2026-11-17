"""9.A gate on fakes: a seeded German PII corpus leaves no trace outside the server.

Every clause of `tests/fixtures/pii/corpus.json` (fictional people only) goes through the
four LLM paths the app has: the judge's decision and its explanation (`make_judge`), the
review summary's recommendation and the rewrite chat. The provider is a strict, recording
Gemini fake: it refuses any plain `str` while masking is on (`require_masked`) and keeps
every payload it was sent. The tracer is a fake Langfuse that applies the boundary's
`langfuse_mask` to every input, output and metadata, as the real SDK does with `mask=`
(`core.langfuse_client` passes it; tests/test_masking.py checks that). Afterwards no PII
value may appear in any recorded payload, the statute texts must arrive verbatim, and what
the lawyer reads (the reasoning, the chat answer) must show the original names again.

No Gemini, Jina or Langfuse call is made.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from core import masking
from core.budget import SharedLimiter
from core.hashing import content_hash
from core.masking import PresidioMasker, boundary
from core.retrieval import Retrieved
from core.schemas import LawChunk

from app.agent import prompts, run, task
from app.domain import (
    ChatIn,
    ClauseSpan,
    ClauseVerdict,
    Decision,
    Document,
    Review,
    Reviewer,
)
from app.review.chat import make_chat_model
from app.review.deliverable import ChatSettings, DeliverableSettings, SummarySettings
from app.review.service import build_service
from app.review.summary import build_summary, make_summary_model
from app.store.memory import memory_store

FIXTURES = Path(__file__).parent / "fixtures" / "pii"
CORPUS = json.loads((FIXTURES / "corpus.json").read_text(encoding="utf-8"))["clauses"]
STATUTES = [
    LawChunk.model_validate(row)
    for row in json.loads((FIXTURES / "statutes.json").read_text(encoding="utf-8"))
]
PII = sorted({value for clause in CORPUS for value in clause["pii"]}, key=len, reverse=True)
MODEL = {"provider": "gemini", "name": "fake-gemini", "max_output_tokens": 400}
ALLOW = ("BGB", "HGB", "AGB", "Verwender", "Kunde", "Käufer", "Verkäufer")


@pytest.fixture(autouse=True)
def _masking_on():
    masking.configure(PresidioMasker(allow_list=ALLOW))
    yield
    masking.configure(None)


def _leaks(payloads: list[str]) -> list[str]:
    return [value for value in PII for payload in payloads if value in payload]


# --- fakes ------------------------------------------------------------------------------


class StrictGemini:
    """A Gemini-shaped client: refuses unmasked text, records every payload, and answers
    by quoting the first person (else any) placeholder it was sent, as a model quoting the
    clause would."""

    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.decisions = SimpleNamespace(create=self._decide)
        self.models = SimpleNamespace(
            generate_content=self._generate, generate_content_stream=self._stream
        )

    def _take(self, text: Any) -> str:
        boundary().require_masked(text)
        self.payloads.append(str(text))
        return str(text)

    def _decide(self, *, model, input, questions):
        self._take(input)
        answer = SimpleNamespace(noul=0.95, confidence=0.95, probabilities=None)
        return SimpleNamespace(
            answers={key: answer for key in questions},
            usage=SimpleNamespace(total_input_tokens=10, total_output_tokens=0),
        )

    def _quote(self, text: str) -> str:
        found = re.search(r"<PERSON_\d+>", text) or masking.PLACEHOLDER.search(text)
        return found.group(0) if found else "die Klausel"

    def _generate(self, *, model, contents, config):
        text = self._take(contents)
        if "recommendation" in json.dumps(config.get("response_json_schema", {})):
            body = {"recommendation": f"Mit {self._quote(text)} nachverhandeln."}
        else:
            body = {"reason_de": f"Die Klausel zu {self._quote(text)} verstößt gegen § 307 BGB."}
        return SimpleNamespace(
            text=json.dumps(body, ensure_ascii=False),
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        )

    def _stream(self, *, model, contents, config):
        text = self._take(contents)
        answer = f"Für {self._quote(text)} schlage ich vor: <vorschlag>Neu.</vorschlag>"
        # Split mid-placeholder on purpose: the unmasker must hold the tail back.
        for start in range(0, len(answer), 7):
            yield SimpleNamespace(text=answer[start : start + 7], usage_metadata=None)
        yield SimpleNamespace(
            text="",
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5),
        )


class MaskingTracer:
    """Fake Langfuse: every input, output and metadata passes `langfuse_mask` (the SDK's
    `mask=` hook) before it is recorded, which is all the real SDK would send."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.count = 0

    def _record(self, **kwargs: Any) -> None:
        for key in ("input", "output", "metadata"):
            if kwargs.get(key) is not None:
                masked = boundary().langfuse_mask(data=kwargs[key])
                self.sent.append(json.dumps(masked, ensure_ascii=False, default=str))

    def start_as_current_observation(self, **kwargs: Any) -> _Observation:
        self._record(**kwargs)
        return _Observation(self)

    start_observation = start_as_current_observation


class _Observation:
    def __init__(self, tracer: MaskingTracer) -> None:
        tracer.count += 1
        self.tracer = tracer
        self.id = f"obs-{tracer.count}"

    def __enter__(self) -> _Observation:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def update(self, **kwargs: Any) -> None:
        self.tracer._record(**kwargs)

    def start_as_current_observation(self, **kwargs: Any) -> _Observation:
        return self.tracer.start_as_current_observation(**kwargs)

    start_observation = start_as_current_observation

    def end(self, **kwargs: Any) -> None:
        return None


def _retrieved(chunk: LawChunk) -> Retrieved:
    return Retrieved(chunk.chunk_id, chunk.citation, chunk.text, 1.0, 1)


def _span(position: int, clause: dict[str, Any]) -> ClauseSpan:
    return ClauseSpan(
        position=position,
        number=str(position + 1),
        heading=clause["heading"],
        text=clause["text"],
        content_hash=content_hash(clause["text"]),
    )


# --- decide and explain -----------------------------------------------------------------


def test_the_judge_sends_and_traces_no_pii_and_shows_the_names_again(monkeypatch):
    prompts.clear_cache()
    monkeypatch.setattr(task, "index_hash", lambda: "index")
    client, tracer = StrictGemini(), MaskingTracer()
    statute = STATUTES[-1]
    judge = run.make_judge(
        {
            "models": {"judge": {"provider": "gemini", "name": "judge"}, "explain": MODEL},
            "prompts": {"label": "evaluation"},
            "retrieval": {"top_k": 1, "search_depth": 1},
            "agent": {"explain_enabled": True},
        },
        limiter=SharedLimiter(),
        client=client,
        tracer=tracer,
        router=lambda text: "liability",
        retriever=lambda *args, **kwargs: [_retrieved(statute)],
    )

    verdicts = [judge(_span(i, clause)) for i, clause in enumerate(CORPUS)]

    # Every clause was decided and explained: two masked requests each.
    assert len(client.payloads) == 2 * len(CORPUS)
    assert _leaks(client.payloads) == []
    assert _leaks(tracer.sent) == []
    assert tracer.sent, "the tracer recorded nothing"
    # The statute reached the model unmangled, and its citation stayed readable.
    explain_prompts = [p for p in client.payloads if "Maßgebliche Normen" in p]
    assert explain_prompts and all(statute.text in p for p in explain_prompts)
    # The reasoning the lawyer reads quotes the real name, not a placeholder.
    reasons = {v.reason_de for v in verdicts}
    assert any("Erika Mustermann" in (reason or "") for reason in reasons)
    assert not any(masking.PLACEHOLDER.search(reason or "") for reason in reasons)


# --- summary and chat -------------------------------------------------------------------


def _service(client: StrictGemini, tracer: MaskingTracer):
    store = memory_store()
    spans = [_span(i, clause) for i, clause in enumerate(CORPUS)]
    store.documents.save(
        Document(
            document_id="doc-pii",
            filename="agb.pdf",
            source_type="pdf",
            page_count=1,
            parser_version="p",
            splitter_version="s",
            text_hash="t",
        ),
        spans,
        {span.content_hash: span.text for span in spans},
    )
    store.reviews.create(
        Review(
            review_id="r-pii",
            document_id="doc-pii",
            agent_version="agent-1",
            scope=["liability"],
            status="finished",
        )
    )
    statute = STATUTES[-1]
    for span in spans:
        store.verdicts.save(
            ClauseVerdict(
                content_hash=span.content_hash,
                agent_version="agent-1",
                verdict="potentially_void",
                legal_basis=[statute.chunk_id],
                reason_de=f"Die Klausel nennt {span.text[:60]} und ist unwirksam.",
                confidence=0.9,
            )
        )
        store.reviews.set_clause_status("r-pii", span.position, "done")
    # A decision note naming people: the summary and the chat both quote notes.
    store.decisions.put(
        Decision(
            review_id="r-pii",
            position=0,
            action="accept",
            note="Rücksprache mit Erika Mustermann (Tel. 0171 2345678) gehalten.",
            decided_by="RA Test",
            model_verdict="potentially_void",
            model_basis=[statute.chunk_id],
        ),
        agent_version="agent-1",
    )
    limiter = SharedLimiter()
    config = {"models": {"summary": MODEL, "chat": MODEL}, "summary": {"llm_enabled": True}}
    return build_service(
        store,
        judge=None,
        limiter=limiter,
        tracer=tracer,
        statutes={chunk.chunk_id: chunk for chunk in STATUTES},
        deliverable=DeliverableSettings(
            summary=SummarySettings(llm_enabled=True, model=MODEL),
            chat=ChatSettings(model=MODEL),
        ),
        summary_model=make_summary_model(config, limiter=limiter, client=client),
        chat_model=make_chat_model(config, limiter=limiter, client=client),
    )


def test_the_summary_recommendation_sends_no_pii():
    client, tracer = StrictGemini(), MaskingTracer()
    summary = build_summary(_service(client, tracer), "r-pii")

    assert len(client.payloads) == 1
    assert _leaks(client.payloads) == []
    assert _leaks(tracer.sent) == []
    assert summary.recommendation_from_llm
    assert not masking.PLACEHOLDER.search(summary.recommendation)


def test_the_chat_streams_no_pii_out_and_the_names_back_in():
    client, tracer = StrictGemini(), MaskingTracer()
    service = _service(client, tracer)
    question = ChatIn(
        message="Formuliere die Klausel ohne Frau Sabine Okonkwo (IBAN DE89 3704 0044 0532 "
        "0130 00) neu.",
        history=[],
    )
    answer = ""
    for position, _clause in enumerate(CORPUS):
        events = list(service.chat("r-pii", position, question, Reviewer(name="RA Test")))
        deltas = [e.payload.get("delta", "") for e in events if e.type == "TEXT_MESSAGE_CONTENT"]
        answer = "".join(deltas)
        assert not masking.PLACEHOLDER.search(answer), answer

    assert len(client.payloads) == len(CORPUS)
    assert _leaks(client.payloads) == []
    assert _leaks(tracer.sent) == []
    # The last clause names nobody, so the model quoted the question's name: restored.
    assert "Sabine Okonkwo" in answer
    # Statute texts are quoted verbatim in every chat prompt.
    assert all(STATUTES[-1].text[:200] in payload for payload in client.payloads)

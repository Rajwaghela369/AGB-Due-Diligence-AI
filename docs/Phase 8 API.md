# Phase 8 API — summary, export, rewrite chat, cost and time

The one source for the Phase 8 Wave 1 builders (export, chat, cost-ui). Models are in
`app/domain.py` and `app/api/schemas.py`, generated into `web/src/api/types.gen.ts`
(`make web-gen`); the chat's events are in `docs/Events.md` ("Rewrite chat"). All paths are
under `/api/v1`. Every error body is `ErrorBody {code, message}` (German `message`, shown
as is); 409 `export_blocked` adds fields. Settings come from `configs/app.yaml` through
`app.review.deliverable.deliverable_settings` (`Service.deliverable`).

Until Wave 1 fills in the bodies, the three routes answer **501 `not_implemented`**
(`NotImplementedError` from `app.review.summary`, `app.review.export`, `app.review.chat`).

## The verdict in force

The summary and the export show what the lawyer decided, not what the model said:

| `finding_status` | verdict in force | basis in force |
| --- | --- | --- |
| `open` (no decision) | the model's | the model's |
| `accepted` | the model's | the model's |
| `overridden` | `corrected_verdict` | `corrected_basis`, or the model's if empty |
| `dismissed` | none: counted, never listed | none |

(`app.domain.decided_verdict`.) A "void finding" is one whose verdict in force is
`potentially_void`; an "open" one has no decision yet.

## `GET /reviews/{review_id}/summary` (8.A)

The summary block, shown on top of the workspace and in the export.

- **Response 200:** `ReviewSummary` (`app/domain.py` documents every field)

  ```jsonc
  {
    "review_id": "…", "document_id": "…", "filename": "Muster-AGB.pdf",
    "agent_version": "…", "review_status": "finished",
    "draft": false,          // a final export would be blocked (see below)
    "counts": {              // SummaryCounts, by the verdict in force
      "findings": 12, "potentially_void": 3, "uncertain": 1, "valid": 6,
      "accepted": 8, "overridden": 2, "dismissed": 2, "open": 0,
      "open_void": 0, "open_review": 0, "failed": 0, "out_of_scope": 4
    },
    "void_groups": [         // void findings by § of their strongest basis
      {"key": "bgb-309", "label": "§ 309 BGB", "findings": [SummaryFinding, …]},
      {"key": "ohne Norm", "label": "ohne Norm", "findings": […]}
    ],
    "top_risks": [SummaryFinding, …],   // ≤ summary.top_risks
    "recommendation": "…",               // German, one paragraph
    "recommendation_from_llm": false,
    "decision_set_hash": "…",            // sha256 hex
    "reviewers": ["Anna Müller"],
    "last_decided_at": "…" | null,
    "generated_at": "…"
  }
  ```

  `SummaryFinding`: `position, number, heading, excerpt, verdict, basis, citations,
  model_verdict, confidence, finding_status, overridden_to_void, decided_by, note`.

- **Rules** (deterministic; a snapshot test pins them):
  - **Groups:** key `"{law}-{paragraph number}"` of the first basis chunk in force
    (`LawChunk.law`, `LawChunk.paragraph`), lower case, e.g. `bgb-309`; label
    `"{paragraph} {law}"`, e.g. `§ 309 BGB`. No basis → `SUMMARY_NO_BASIS`. Groups by size
    (largest first), then `key`; findings in document order.
  - **Top risks:** verdict in force `potentially_void` or `uncertain`. Rank 0: lawyer
    confirmed void (accepted void, or `overridden_to_void`); rank 1: open void; rank 2:
    uncertain. Within a rank by model `confidence` descending, then position.
  - **Excerpt:** clause text cut at a word boundary to `summary.excerpt_chars`, with „…“.
  - **`draft`:** `open_void + open_review > 0`, or `review_status` is not `finished` /
    `awaiting_decisions`.
  - **Recommendation:** a rule-based German paragraph from the counts and groups (e.g. how
    many clauses to rewrite or remove, citing the groups). With `summary.llm_enabled`
    (**off by default**) it is replaced by one generated paragraph (`models.summary`, the
    deterministic summary as its only input), cached in `summary_texts` by
    (`decision_set_hash`, model): one call per decision set, never repeated. A model
    failure keeps the rule-based text, `recommendation_from_llm: false`, caches nothing,
    and is not an error.
  - **`decision_set_hash`:** sha256 of the canonical JSON (sorted keys, no whitespace) of
    the deterministic fields: `agent_version`, `counts`, `void_groups`, `top_risks`
    (`generated_at`, the recommendation fields and the hash itself excluded). Any
    decision, note or verdict change that the summary shows changes it.
- **Errors:** 404 `not_found` (unknown review). Works in every status; a running review
  gives `draft: true`.
- The UI refetches it after every decision (`DecisionOut`) and on `RUN_FINISHED`.

## `GET /reviews/{review_id}/export?format=docx|md|csv&draft=false` (8.B)

The DD-ready export, as a file download.

- **Query:** `format` (required): `docx`, `md` or `csv`; `draft` (optional, default
  `false`).
- **Headers:** `X-Reviewer` (optional, as for decisions: percent-encoded UTF-8): who
  exports, named on the cover („Unbekannt“ without it, marked „nicht angemeldet“).
- **Response 200:** the file.

  | header | value |
  | --- | --- |
  | `Content-Type` | `EXPORT_MEDIA_TYPES[format]` (DOCX MIME; `text/markdown; charset=utf-8`; `text/csv; charset=utf-8`) |
  | `Content-Disposition` | `attachment; filename*=UTF-8''<percent-encoded filename>` |
  | `X-Export-Draft` | `true` or `false` |
  | `Cache-Control` | `no-store` |

  CORS exposes `Content-Disposition` and `X-Export-Draft`. Filename:
  `AGB-Check_<document stem>_<YYYY-MM-DD>[_Entwurf].<ext>` (date of the export, Europe/Berlin;
  stem with path separators and control characters removed).

- **Blocking:** without `draft=true` the export is refused while
  - the review is `queued`, `running` or `failed` → 409, `reason: "not_finished"`,
    `blocking: []`; or
  - any finding with model verdict `potentially_void` or `uncertain` has no decision
    (`counts.open_void + counts.open_review > 0`) → 409, `reason: "undecided"`, `blocking`
    lists them in document order.

  ```jsonc
  // 409, ExportBlockedBody
  {
    "code": "export_blocked",
    "message": "Offene Feststellungen: …",
    "reason": "undecided",
    "blocking": [{"position": 3, "number": "6.1", "heading": "§ 6 Haftung", "verdict": "uncertain"}]
  }
  ```

  `draft=true` is never blocked. Every draft is watermarked „Entwurf“
  (`DRAFT_WATERMARK`): DOCX diagonally on every page (section header), Markdown as the
  first line `> **ENTWURF** – …` and in the title, CSV in the `entwurf` column (`ja`).
  Undecided findings appear in a draft with decision „offen“.

- **Content** (configs/export/README.md has the memo layout):
  - **DOCX** (`python-docx`, template `export.template_dir/memo.docx`) and **Markdown**:
    cover (document, review date, exporter, reviewers, `agent_version`,
    `EXPORT_DISCLAIMER`), the summary, a table of findings with a verdict (clause number
    and excerpt, decision, verdict in force, § citations, reasoning, note, decided by,
    decided at), and an appendix with the text of every cited statute chunk (§ order).
    The reasoning is `Decision.reason_de` when set, else the verdict's `reason_de`.
  - **CSV:** UTF-8 with BOM, `;` separator, CRLF, RFC 4180 quoting; one row per finding
    with a verdict, in document order. Columns:
    `position;nummer;ueberschrift;klausel;modell_ergebnis;modell_konfidenz;entscheidung;ergebnis;normen;begruendung;notiz;entschieden_von;entschieden_am;agent_version;entwurf`
    (`entscheidung` ∈ `übernommen|geändert|verworfen|offen`; `ergebnis` the verdict in
    force; `normen` citations joined by ` | `; dates ISO 8601 UTC).
  - No model call: the summary's cached LLM paragraph is used only if it already exists.
  - Deterministic for the same review, decisions, exporter and date (golden files):
    DOCX zip timestamps and core properties are fixed to the review's `finished_at`.
- **Errors:**

  | status | code | when |
  | --- | --- | --- |
  | 404 | `not_found` | unknown review |
  | 409 | `export_blocked` | see Blocking |
  | 422 | `invalid_request` | unknown `format` |
  | 422 | `invalid_reviewer` | bad `X-Reviewer` |
  | 501 | `not_implemented` | until Wave 1 |

  Service exceptions: `KeyError` → 404, `ExportBlocked` → 409.

## `POST /reviews/{review_id}/clauses/{position}/chat` (8.C)

One rewrite question about one finding („Formuliere 6.1 rechtskonform um“). One model
call per message; the answer streams as SSE.

- **Headers:** `Content-Type: application/json`, `Accept: text/event-stream`,
  `X-Reviewer` (optional). The browser reads the stream with `fetch` (EventSource cannot
  POST): `api.reviews.chat(...)` returns the raw `Response`.
- **Request:** `ChatIn`

  ```jsonc
  {
    "message": "Formuliere 6.1 rechtskonform um",   // 1–2000 characters
    "history": [                                    // ≤ 20, oldest first; optional
      {"role": "user", "content": "…"},
      {"role": "assistant", "content": "…"}         // ≤ 8000 characters each
    ]
  }
  ```

  The server stores no chat. The client keeps the turns and sends them back; only the last
  `chat.max_history_messages` reach the prompt, and all of them are quoted as data.
- **Response 200:** `text/event-stream`, the sequence in `docs/Events.md` ("Rewrite
  chat"), ending with `CUSTOM chat_result` (`ChatResult`) and `RUN_FINISHED`, or
  `RUN_ERROR`.

  ```jsonc
  // ChatResult
  {
    "message_id": "…",
    "text": "…",                 // whole answer, plain text, ≤ chat.max_answer_chars
    "suggestion": "…" | null,    // the proposed clause wording, if any
    "diff": [{"op": "equal" | "insert" | "delete", "text": "…"}],  // original → suggestion
    "truncated": false,
    "disclaimer": "KI-generierter Formulierungsvorschlag, keine Rechtsberatung. …",
    "model": "gemini-3.8-flash",
    "input_tokens": 812, "output_tokens": 240, "cost_usd": 0.0, "latency_ms": 2140
  }
  ```

- **Guardrails** (the chat slice implements them in `app/review/chat.py`):
  - **Context:** the clause and its heading, quoted between the explain step's
    `<klausel>`…`</klausel>` delimiters with lookalikes defanged
    (`app.agent.prompts._DELIMITER_LOOKALIKE`); the cited statute texts (ours, outside the
    quote); the decision (verdict in force, §, note, also quoted) and the reasoning. The
    history and the message are quoted as data in their own delimited blocks. The system
    prompt says that only the system prompt gives instructions.
  - **Length:** `chat.max_output_tokens` for the model, `chat.max_answer_chars` for the
    kept answer (`truncated: true` when either cut it).
  - **Suggestion:** the model wraps the proposed wording in `<vorschlag>`…`</vorschlag>`;
    the server extracts it (absent → `suggestion: null`, `diff: []`), strips markup, and
    diffs it word by word against `ClauseSpan.text` (`difflib`; `equal` + `delete` =
    original, `equal` + `insert` = suggestion).
  - **Disclaimer:** `CHAT_DISCLAIMER` on every result; the UI shows it under the answer.
  - **Cost:** `cost_usd` at the configured prices; not added to `Review.cost_usd`.
- **Allowed on:** clauses with a verdict (`done` or `awaiting_decision`).
- **Errors before streaming** (JSON):

  | status | code | when |
  | --- | --- | --- |
  | 404 | `not_found` | unknown review or position |
  | 409 | `not_chattable` | the clause has no verdict |
  | 422 | `invalid_request` | body fails validation |
  | 422 | `invalid_reviewer` | bad `X-Reviewer` |
  | 429 | `rate_limited` | the per-IP limiter (shared with reviews and explain) |
  | 429 | `daily_budget` | `BudgetExceeded` from the shared limiter |
  | 501 | `not_implemented` | until Wave 1 |
  | 503 | `service_unavailable` | no chat model configured, or shutting down |

  After the first byte a failure is a `RUN_ERROR` event (`code`: `chat_failed` or
  `daily_budget`), never an HTTP error.

## Cost and time (8.D)

Derived on read (`app/api/costs.py`); nothing new is stored.

- **Prices:** `Pricing` from `configs/app.yaml`: `review.price_usd_per_million_tokens`
  (`input`, `output`) and `review.eur_per_usd`. **Both prices are still 0 placeholders and
  the rate is null (TODO in the config)**, so `confirmed: false`: the UI shows „Preise
  nicht hinterlegt“ instead of 0 €, and shows USD while `eur_per_usd` is null.
- **`ReviewOut`** (every route that returns it) gains:

  | field | value |
  | --- | --- |
  | `pricing` | `Pricing` |
  | `cost_eur` | `review.cost_usd * eur_per_usd`; null without a rate |
  | `wall_time_ms` | `finished_at - started_at` when idle (the end of the last run: a decision moving `awaiting_decisions → finished` no longer restamps `finished_at`); `now - started_at` while running; null before the first start |
  | `cache_hit_rate` | (unchanged) `counts.cache_hits / counts.total` |

  `review.cost_usd` is what this review paid: judge and explain calls of clauses it judged
  itself (not cache hits) plus decision resumes. Chat and summary calls are not in it.
- **`DocumentList`** (home) gains `pricing`; each `DocumentListItem` gains
  `cache_hit_rate`, `cost_eur` and `wall_time_ms` of its `latest_review`.
- **Per clause** (Technical details): from the verdict, which already stores
  `input_tokens`, `output_tokens` and `latency_ms` (judge + explain of the run that judged
  it): `usd = (input_tokens * input_usd_per_million + output_tokens *
  output_usd_per_million) / 1e6`, `eur = usd * eur_per_usd`. Verdicts are shared, so on
  a cache hit these are the figures of the original judging, which this review did not
  pay. There is no per-clause cache-hit flag, so the label reads „Kosten der Erstprüfung“
  for every clause, and the per-clause sum need not equal the review's `cost_usd`.

## Service signatures (Wave 1 implements; `app/review/interfaces.py`)

```python
def summary(self, review_id: str) -> ReviewSummary
def export(self, review_id: str, fmt: ExportFormat, *, draft: bool = False,
           reviewer: Reviewer) -> ExportFile
def chat(self, review_id: str, position: int, message: ChatIn, reviewer: Reviewer,
         *, request_id: str | None = None) -> Iterator[ChatEvent]
```

`Service` delegates each to its slice's module: `app.review.summary.build_summary(service,
review_id)`, `app.review.export.build_export(service, review_id, fmt, draft=…,
reviewer=…)`, `app.review.chat.start_chat(service, review_id, position, message,
reviewer, request_id=…)`. Route handlers get the reviewer with
`app.api.deps.reviewer(request)`.

# Phase 5–6 Agent Plan

Nov 16, 2026. How [Phase 5](Phase%205%20Plan.md) (backend API) and [Phase 6](Phase%206%20Plan.md)
(frontend) are built: which sub-agent does which task, on which model, in what order.

## Working model

- **One orchestrator.** The main Claude Code session (Opus 5.5) fixes the contracts, starts
  every agent, reviews and merges every branch, and runs the live gates. Agents never merge.
- **Two kinds of worker.**
  - *Claude agents* run as Claude Code sub-agents, each in its own git worktree.
  - *Codex agents* run headless: `codex exec -C <worktree> -s workspace-write -m <model>
    -c model_reasoning_effort="medium" -o <report>`, in the background.
- **One branch per agent**, cut from `stage` (`p5/store`, `p5/ingest`, …). Worktrees live
  in `../agb-check-wt/<agent>`.
- **At most three agents at once**, mixing Claude and Codex. A wave starts only after the
  previous one is reviewed, merged into `stage`, and `make check` is green.
- **Cross-review.** Claude-written branches are reviewed with `codex exec review`;
  Codex-written branches are reviewed by the orchestrator.
- **Fakes only inside agents.** Agents run no live calls to Gemini, Jina, Langfuse or Neon.
  Live gate checks (nested trace, feedback score, full PDF run) are run by the orchestrator.
- **Owned files.** `pyproject.toml`, `uv.lock`, `Makefile`, `.importlinter` and the Wave 0
  contract modules belong to the orchestrator. An agent that needs a change there asks for
  it in its report instead of editing. Dependencies are installed before an agent starts,
  because the Codex sandbox has no network.
- **Codex models:** the gpt-5.6 family at medium reasoning effort, never high. *sol* for
  hard work, *terra* for straightforward work.

## Contracts (Wave 0)

Fixed before any agent starts, so parallel branches meet at known interfaces:

| Module | Fixes |
| --- | --- |
| `app/domain.py` | Clause spans with rectangles/offsets, statuses, review, finding, feedback and decision types, AG-UI event names and schema version |
| `app/store/interfaces.py` | Repository and `FileStore` protocols the store implements and everyone else consumes |
| `app/ingest/__init__.py` | `ingest()` / `split()` signatures |
| `app/review/interfaces.py` | `ReviewService` protocol the API calls |
| `app/agent/run.py` | `judge_clause()` signature: parent span in, step observation ids out |
| `core/budget.py` | `SharedLimiter` signature: one rate limit and daily budget per server |
| `app/api/schemas.py` | Request and response models, so the OpenAPI schema (and the Phase 6 client) is stable early |

## Allocation

### Phase 5 — Backend

| Wave | Agent | Runs on | Model | Tasks |
| --- | --- | --- | --- | --- |
| 0 | contracts | orchestrator | Opus 5.5 | Contracts above, dependencies, import-linter rules, worktrees |
| 1 | **store** | Claude | Sonnet 5.5 | 5.A — migrations, Postgres repositories, in-memory fakes, `verdicts` re-key, `decisions`, eval harness on `content_hash` |
| 1 | **ingest** | Codex | gpt-5.6-sol · medium | 5.B — validate, Gotenberg convert, sandboxed pdfplumber parse, German AGB splitter, draft gold set |
| 1 | **core-hooks** | Codex | gpt-5.6-terra · medium | `judge_clause()` with parent span and observation ids, embedding/retrieval caches, `SharedLimiter` |
| 2 | **runner** | Claude | Opus 5.5 | 5.C + `events.py` — cache-first runner, dedupe, concurrency, per-clause failure, retry, resume, event log and fan-out |
| 2 | **api** | Codex | gpt-5.6-sol · medium | 5.D routes, SSE with `Last-Event-ID` replay, tracing; 5.E feedback and decisions |
| 2 | **web-foundation** | Codex | gpt-5.6-sol · medium (network on) | 6.A — runs early against the frozen OpenAPI schema and mocks |
| 3 | **ops** | Claude | Haiku 5.5 | 5.F — upload hardening, CORS, limits, JSON logs, `/ready`, Docker Compose, `make api`, `configs/app.yaml` |
| gate | live checks | orchestrator | Opus 5.5 | Phase 5 gate against real services |

### Phase 6 — Frontend

| Wave | Agent | Runs on | Model | Tasks |
| --- | --- | --- | --- | --- |
| 4 | **viewer** | Claude | Opus 5.5 | 6.C — pdf.js, highlight layer, live clause states, toolbar |
| 4 | **panel** | Claude | Sonnet 5.5 | 6.D + 6.F — findings cards, drawer, statute text, feedback |
| 4 | **home-table** | Codex | gpt-5.6-terra · medium | 6.B + 6.E — home, upload, error states, virtualised table |
| 5 | **gate** | Codex | gpt-5.6-terra · medium | 6.G — keyboard, accessibility, dark mode, Playwright end-to-end in CI |
| gate | live checks | orchestrator | Opus 5.5 | Phase 6 gate |

## Schedule

```
Wave 0  orchestrator: contracts
Wave 1  store (Claude) ║ ingest (Codex) ║ core-hooks (Codex)            → review, merge
Wave 2  runner (Claude) ║ api (Codex) ║ web-foundation (Codex)          → review, merge
Wave 3  ops (Claude)                                                    → Phase 5 gate
Wave 4  viewer (Claude) ║ panel (Claude) ║ home-table (Codex)           → review, merge
Wave 5  gate (Codex)                                                    → Phase 6 gate
```

The orchestrator reports after every wave: what merged, test results, open problems.

## Decisions taken

The recommended option from each plan's open decisions:

- PDF library: **pdfplumber** (MIT).
- File storage: **local disk** behind `FileStore`; EU object storage in Phase 9.
- Login: **none in v1**; audit columns exist for later.
- Interface language: **German first**, English as a second file later.
- AG-UI client: plain **`@ag-ui/client`**.

## Needs a human

- **Splitter gold set.** The ingest agent drafts clause boundaries for 5–10 public shop-AGB
  PDFs in `data/gold/`; they must be checked by hand before the 95% target means anything.
- **Docker.** Gotenberg and the Compose stack need Docker Desktop running; other
  containers may be stopped while this app's services run.

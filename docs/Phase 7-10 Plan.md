# Phase 7–10 Plan — Decisions, deliverable, DSGVO, AVV

Nov 16, 2026. The remaining roadmap from `docs/Development Phases.md`, broken into tasks with a
goal, implementation points and "done when", plus the agent allocation used for Phases 5–6.
The core path is **7 → 9**. If time runs short, cut Phase 8 scope and drop Phase 10, as the
roadmap says.

## Where Phases 5–6 leave us

Already built, and reused below:

- `decisions` table and `PUT /reviews/{id}/clauses/{pos}/decision` (accept / override /
  dismiss + note). Accept and override already record feedback, so Phase 7 builds the UI and
  the audit trail on a mechanism that exists.
- Feedback scored on the agent step it concerns, with stable score ids.
- One Langfuse trace per review; `agent_version` on every verdict, review and feedback row.
- `FileStore` interface (local disk), `uploads.uploaded_by` and the decision columns are
  ready for a user id.
- `make check` with Playwright end to end on fake model clients.

Carried over from Phase 5–6 and handled first, in Phase 7 Wave 0:

| Carry-over | Why it matters now |
| --- | --- |
| Topic tagging at ingest (`clause.topic` via the router) | 23 of 36 clauses came back `uncertain` because every clause is judged; the scope filter is a no-op. With topics, out-of-scope clauses are skipped and the lawyer's queue shrinks. |
| Splitter gold set hand-checked | A bad split makes every downstream verdict and decision wrong. |
| CI: Playwright browser install in `.github/workflows/ci.yml` | `make check` now needs Chromium. |
| Gemini token prices in `configs/app.yaml` | Phase 8 shows cost per clause and per review. |

## Team: Claude and Codex together

The build runs as one team of two vendors, coordinated by one orchestrator.

| Role | Who | Model | Used for |
| --- | --- | --- | --- |
| Orchestrator | Claude | Opus 5.5 | Wave 0 contracts, merges, live gates, `HANDOFF.md` |
| Hard builder | Claude | Opus 5.5 | concurrency, agent graph, security-critical code |
| UI builder | Claude | Sonnet 5.5 | React features, interaction, accessibility |
| Light builder | Claude | Haiku 5.5 | ops, config, docs, snapshot tooling |
| Backend builder | Codex | gpt-5.6-sol · medium | well-specified API, store and pipeline work |
| Mechanical builder | Codex | gpt-5.6-terra · medium | tests, table/form UI, refactors, migrations |
| Fallback orchestrator | Codex | gpt-5.6-sol · medium | takes over from `HANDOFF.md` if the Claude session runs out |

**Working agreement.**

1. **Cross-vendor pairs.** Every slice has a builder from one vendor and a reviewer from the
   other. A Codex review runs read-only (`codex exec -s read-only` on `git diff stage...HEAD`);
   a Claude review is a Sonnet sub-agent or the orchestrator. The builder fixes the
   findings, and only then does the orchestrator merge.
2. **Contracts before code.** Wave 0 fixes the interfaces. Shared files have one owner;
   others ask in their report.
3. **Small waves.** At most three builders at once, mixed across vendors, and one branch and
   worktree per slice.
4. **Availability check.** Before each wave, a 1-token `codex exec` probe. If Codex is
   rate-limited, its slices move to Claude Sonnet (or Opus for sol-level work), and Codex
   reviews resume once it is back.
5. **Fakes only inside agents.** No `.env` in worktrees. Live checks are run by the
   orchestrator only, within the Gemini cap.
6. **Handoff always current.** `agb-check-wt/HANDOFF.md` is updated after every merge, so
   either vendor can take over orchestration.

## Standing rules

- **At most 5 Gemini calls in any test or live check.** Agents use fakes only. Live checks
  reuse cached documents or 1–2 pasted clauses. Continuous-evaluation runs (tens to hundreds
  of calls) happen only when the user explicitly approves each one.
- Contracts first (Wave 0, orchestrator), then parallel agents in worktrees, at most three
  at a time, then cross-review and merge.
- Before giving Codex work, check that Codex is available (it hit its usage limit on
  2026-11-16); otherwise use Claude.

---

## Phase 7 · Human in the loop

**Goal:** the result of a review is the lawyer's judgement, recorded and defensible, not the
model's output.

**Gate:**

- an interrupted run survives a page reload and resumes correctly on the lawyer's decision
- every decision is visible in the audit trail (who, what, when, against which
  `agent_version`) and as a Langfuse score
- lawyer agreement per `agent_version` is shown and recorded in `docs/Metrics.md`
- `make check` green, including a Playwright test: open → decide → reload → decision kept

### 7.0 · Wave 0 (orchestrator)

Contracts: finding status enum, decision history shape, the interrupt event in
`docs/Events.md`, and API models for progress and the agreement metric. Plus the carry-overs
above: topic tagging (in ingest or at review start, using `core.routing`; bump
`SPLITTER_VERSION` only if the split changes) and the CI fix.

### 7.A · Finding status, notes, review progress

**Goal:** the lawyer works through a review like a checklist and always sees how far they are.

**Implementation.**

- Finding status derived per review position: **Open** (no decision), **Accepted**,
  **Overridden**, **Dismissed**. Shown on cards, in the table, as a filter, and on the home
  list.
- `A` / `O` / `D` hotkeys (reserved in Phase 6) act on the selected finding. Override opens a
  verdict + § picker. Every action can take a note.
- Progress bar: decided / in-scope findings, with void and review findings counted
  separately. „Prüfung abschließen" is enabled when every void and review finding is decided.
- `GET /reviews/{id}` gains `decided` counts; the home list shows progress.

**Done when:** each status is reachable by mouse and keyboard in a component test, and
progress updates without a reload.

### 7.B · Graph interrupt on `uncertain`, Postgres checkpointer

**Goal:** an uncertain clause waits for the lawyer instead of being reported as if settled.

**Implementation.**

- Add `langgraph-checkpoint-postgres`. The graph interrupts after `self_check` when the
  verdict is `uncertain`; `thread_id` = `{review_id}:{position}`, checkpoints in Neon.
- The runner marks the clause `awaiting_decision` and emits a new `STATE_DELTA` status (and
  an `INTERRUPT` custom event). The review can finish while clauses still await decisions;
  its status becomes `awaiting_decisions`.
- A decision resumes the graph with the lawyer's verdict (and §). For a confirmed or
  overridden void verdict, the explanation step runs on the lawyer's verdict, so the
  reasoning matches the decision. The verdict row stays the model's; the decision is
  separate.
- **Cache interplay:** a cached `uncertain` verdict has no live graph to resume. Treat it
  as `awaiting_decision` at review level; resuming then means "record the decision and, if
  void, run explain only". Document this in `docs/Events.md`.
- Resume works across restarts (checkpoint in Postgres) and page reloads (state from the
  store).

**Done when:** a fake-judge test interrupts, the service is restarted, and the decision
resumes the right checkpoint. The Playwright test reloads mid-interrupt and decides.

### 7.C · One feedback mechanism, lawyer agreement

**Goal:** every decision teaches the system something measurable.

**Implementation.**

- Accept = 👍 on the clause span; override = 👎 `verdict_wrong` (+ `basis_wrong` when the §
  changes); dismiss = a `dismissed` score (finding not relevant), not counted as
  disagreement. This already exists in `FeedbackService.decide`; extend it for § overrides.
- Agreement per `agent_version`: share of decided findings where the lawyer kept the model's
  verdict, overall and per verdict class (void precision as the lawyer sees it). Served from
  a SQL view, `GET /metrics/agreement`, and shown on home.
- Recorded as a Langfuse score per review (`lawyer_agreement`), and added as a section in
  `docs/Metrics.md`.

**Done when:** a seeded test with known decisions yields the exact agreement figures, and
re-deciding updates rather than double-counts.

### 7.D · Audit trail

**Goal:** a reviewer can prove who decided what, when and against which model version.

**Implementation.**

- `decision_events`: an append-only table, written in the same transaction as the
  `decisions` upsert (who, action, from/to verdict and §, note, `agent_version`, timestamp,
  request id). Never updated or deleted.
- "Who" before login (Phase 9): a reviewer name set once in the UI and sent as a header,
  marked unauthenticated in the trail.
- Audit view in the drawer ("Verlauf") and `GET /reviews/{id}/audit`.

**Done when:** changing a decision three times leaves three history rows and one current
decision, and the drawer shows all of them.

### 7.E · Corrected verdicts to a Langfuse dataset

**Goal:** lawyer corrections become evaluation data without manual copying.

**Implementation.**

- On override, add an item to a Langfuse dataset `agb-lawyer-corrections`: clause text,
  model verdict, corrected verdict and §, `agent_version`, and a source review link. Make it
  idempotent on (content_hash, review). Behind a config switch, **off for real client
  documents until Phase 9 masking exists**.
- `make eval-corrections` runs the agent on that dataset. It needs explicit user approval
  each time (Gemini cap).

**Done when:** a fake-Langfuse test shows one dataset item per override, updated (not
duplicated) on re-override.

### Phase 7 agents

| Wave | Slice | Builder | Model | Reviewer | Tasks |
| --- | --- | --- | --- | --- | --- |
| 0 | contracts | orchestrator | Claude Opus | — | contracts, topic tagging, CI |
| 1 | **interrupt** | Claude | Opus | Codex sol | 7.B runner, graph, checkpointer, events |
| 1 | **decisions-api** | Codex | gpt-5.6-sol | Claude Sonnet | 7.C, 7.D, 7.E backend |
| 1 | **decisions-ui** | Claude | Sonnet | Codex terra | 7.A UI, A/O/D hotkeys, progress, audit view |
| 2 | **gate7** | Codex | gpt-5.6-terra | Claude (orchestrator) | Playwright reload-and-resume, agreement figures |
| gate | live | orchestrator | Claude Opus | — | ≤ 5 Gemini calls |

---

## Phase 8 · Polish and deliverable

**Goal:** from a raw document to a DD-ready export without leaving the tool.

**Gate:** a complete DD-ready export (memo + CSV) from a raw document, built only from the
lawyer's decisions, with date, reviewer and `agent_version`.

### 8.A · Summary block

Void clauses grouped by paragraph, top risks (void and overridden-to-void first, by
confidence), counts, and a recommendation. The block is **deterministic** from decisions;
an optional one-paragraph LLM recommendation (1 call, cached by decision set hash) sits
behind `summary.llm_enabled`. Shown on top of the workspace and in the export.
**Done when:** the summary for a seeded review matches a snapshot, and changes when a
decision changes.

### 8.B · Export

`GET /reviews/{id}/export?format=docx|md|csv`. DD memo: cover (document, date, reviewer,
`agent_version`, disclaimer), summary, a table of decided findings (clause, decision, §,
reasoning, note), and an appendix with statute texts. Built with `python-docx` and a
template in `configs/export/`. CSV is one row per finding. Undecided void and review findings
block the export unless the lawyer exports as a draft, which is watermarked „Entwurf".
**Done when:** golden-file tests for all three formats, and the DOCX opens in Word and
LibreOffice (checked through Gotenberg in CI).

### 8.C · Follow-up chat for rewrites

A per-finding chat in the drawer: „Formuliere 6.1 rechtskonform um". The context is the
clause, the cited statute texts, the decision and the reasoning. Answers stream over SSE.
Guardrails: the clause is quoted as data (the same delimiters as explain), the output is
length-capped, there is a disclaimer, and the suggested text is diff-highlighted against the
original. Each message costs 1 Gemini call, so live tests stay within the cap.
**Done when:** fake-model tests for streaming, delimiting and the diff; one live check of at
most 2 calls.

### 8.D · Cost and latency in the UI

Per clause (tokens, € from `configs/app.yaml` prices, latency) in Technical details; per
review (total €, wall time, cache-hit share) in the header and on home. Needs the
carry-over prices.
**Done when:** the numbers match the stored verdicts and review row in a component test.

### 8.E · Trigger-phrase highlighting (optional)

Only if continuous evaluation shows it is reliable. Ask the judge which span triggers the
verdict, and underline it inside the clause highlight. Otherwise skip.

### Phase 8 agents

| Wave | Slice | Builder | Model | Reviewer | Tasks |
| --- | --- | --- | --- | --- | --- |
| 0 | contracts | orchestrator | Claude Opus | — | summary and export models, chat events |
| 1 | **export** | Codex | gpt-5.6-sol | Claude Sonnet | 8.A backend, 8.B DOCX, Markdown and CSV |
| 1 | **chat** | Claude | Opus | Codex sol | 8.C rewrite chat (prompt safety, streaming) |
| 1 | **cost-ui** | Codex | gpt-5.6-terra | Claude Haiku | 8.D, summary block UI |
| 2 | **gate8** | Claude | Sonnet | Codex terra | Playwright raw document → export, golden files |
| gate | live | orchestrator | Claude Opus | — | ≤ 5 Gemini calls (1 summary + 2 chat) |

---

## Phase 9 · DSGVO hardening

Required before any real client AGB enters the system.

**Gate:** no unmasked personal data in Langfuse or in any LLM request; a deleted document
leaves nothing behind that only it used; every store confirmed EU-hosted; per-user access
enforced.

### 9.A · PII masking

Mask personal data (names, addresses, emails, phone numbers, IBANs, company register
numbers of natural persons) **before every LLM call and before trace storage**, and
reversibly for display only. Use Presidio with a German spaCy model plus regexes for IBAN,
phone and email, and Langfuse's `mask` hook on the client. A clause's content hash stays on
the unmasked text (cache identity); the masked text is what leaves the server.
**Done when:** a seeded PII corpus produces zero hits in captured LLM payloads and
fake-Langfuse traces, and verdicts on the AGB-DE dev slice don't change (eval with explicit
approval).

### 9.B · Deletion with reference counting

`DELETE /documents/{id}` removes the uploads, `document_clauses`, the file, reviews, events,
decisions and feedback for that document. Clause texts, verdicts, embeddings and retrievals
are removed **only** when no other document references the content hash. This is one
transaction, plus a nightly orphan sweep. The audit trail keeps a tombstone (who deleted
what, when) without content.
**Done when:** a property test over random documents with shared clauses leaves exactly the
expected rows, and Langfuse traces for the document are deleted through the API.

### 9.C · Retention policy

Configurable retention per data class (files, reviews, traces). A scheduled job deletes
expired data through 9.B. Documented in `docs/DSGVO.md`.

### 9.D · EU object storage

An S3-compatible `FileStore` implementation (e.g. Hetzner, Scaleway or OVH, EU region),
selected by config, with server-side encryption, a migration script from local disk, and
signed URLs or proxied downloads.

### 9.E · Login and per-user access

OIDC login (the provider is an open decision), an `owner_id` on documents and reviews,
row-level checks in every repository method, and "who" in the audit trail from the session
instead of a header. The single-worker limit is lifted in the same phase: `LISTEN/NOTIFY` for
events, a Postgres counter for the limiter, and `FOR UPDATE SKIP LOCKED` for the run queue.

### 9.F · AVV / Art. 28 checklist

`docs/DSGVO.md`: each processor (Neon, Qdrant, Gemini, Jina, Langfuse, object storage) with
region, AVV status, sub-processors and data categories. Marked confirmed only with a link to
the signed AVV.

### Phase 9 agents

| Wave | Slice | Builder | Model | Reviewer | Tasks |
| --- | --- | --- | --- | --- | --- |
| 0 | contracts | orchestrator | Claude Opus | — | masking boundary, owner model, deletion contract |
| 1 | **masking** | Claude | Opus | Codex sol | 9.A Presidio + regex, Langfuse mask hook |
| 1 | **deletion** | Codex | gpt-5.6-sol | Claude Opus | 9.B reference-counted deletion, 9.C retention |
| 1 | **storage** | Codex | gpt-5.6-terra | Claude Haiku | 9.D S3-compatible EU `FileStore` |
| 2 | **auth-scale** | Claude | Opus | Codex sol | 9.E OIDC, owner checks, LISTEN/NOTIFY, job queue |
| 2 | **dsgvo-doc** | Claude | Sonnet | Codex terra | 9.F processor checklist (research) |
| 3 | **gate9** | Codex | gpt-5.6-sol | Claude (orchestrator) | PII-leak, deletion-property and access tests |
| gate | live | orchestrator | Claude Opus | — | masked payloads verified live, ≤ 5 Gemini calls |

---

## Phase 10 · AVV checker (stretch)

**Goal:** the same machinery checks a data processing agreement against Art. 28(3) DSGVO.

1. **10.A · Norm set:** DSGVO Art. 28 (and Art. 32 references) into the knowledge base
   through P1 with a new source and collection alias (`dsgvo_avv`).
2. **10.B · Labelled set:** 30–50 hand-labelled AVV clauses (public templates),
   held-out split, in a Langfuse dataset.
3. **10.C · Agent profile:** a second app config (questions: „Erfüllt die Klausel Art.
   28 Abs. 3 lit. x?"), a router over the lit. a–h obligations, and a missing-obligation check
   at document level (an AVV that never mentions lit. c is a finding too).
4. **10.D · UI:** a document type chosen at upload; the same workspace, with findings per
   obligation.

**Done when:** an F1 on the held-out AVV set is recorded in `docs/Metrics.md` (with
approval for the Gemini calls), and the UI runs an AVV end to end on fakes.

### Phase 10 agents

| Wave | Slice | Builder | Model | Reviewer | Tasks |
| --- | --- | --- | --- | --- | --- |
| 0 | contracts | orchestrator | Claude Opus | — | AVV profile config, obligation model |
| 1 | **avv-kb** | Codex | gpt-5.6-sol | Claude Sonnet | 10.A DSGVO norm set through P1, 10.B labelled set |
| 1 | **avv-agent** | Claude | Opus | Codex sol | 10.C questions, router, missing-obligation check |
| 2 | **avv-ui** | Codex | gpt-5.6-terra | Claude Sonnet | 10.D document type, per-obligation findings |

---

## Continuous evaluation (all phases)

Void recall above 0.70 and F1 above 0.276, run as Langfuse experiments. From Phase 7 on it
is fed by lawyer corrections (7.E). Every run needs the user's explicit approval, because of
the Gemini cap. Never blocks a phase.

## Before Phase 7: UI fixes

`docs/UI Fix Plan.md` (17 bugs from the Phase 6 screenshot review) runs first, in two waves:
UF-A (Claude Sonnet) ║ UF-B (Codex sol) ║ UF-C (Codex terra), then UF-D (Claude Sonnet) ║
UF-E (Claude Haiku). Phase 7 builds its decision UI into the same workspace and drawer.

## Sequencing and effort

| Phase | Waves | Agents | Rough size | Depends on |
| --- | --- | --- | --- | --- |
| UI fixes | 2 | 5 | small | — |
| 7 | 0 + 2 | 4 | like Phase 5 | carry-overs |
| 8 | 0 + 2 | 4 | smaller than 6 | 7 (decisions feed the export) |
| 9 | 0 + 3 | 6 | the largest | 7 (audit "who"); must precede real client data |
| 10 | 1–2 | 2–3 | small | 9 for real AVVs; can run on public templates earlier |

Core path: **7 → 9**. Phase 8 can run in parallel with 9's later waves if agents are free.

## Open decisions

1. **Interrupt vs. post-hoc decisions (7.B):** the roadmap asks for a graph interrupt. Since
   most uncertain verdicts come from the verdict cache (no live graph), a review-level
   "awaiting decision" state does most of the work. Recommended: build both as described,
   with the interrupt only on live runs.
2. **Login provider (9.E):** a self-hosted Keycloak (EU, full control), or a managed OIDC
   provider with EU data residency. Recommended: Keycloak if the firm has an IdP to federate
   with, otherwise managed.
3. **Object storage provider (9.D):** Hetzner, Scaleway or OVH (all EU). Recommended:
   whichever the firm already has an AVV with.
4. **LLM summary recommendation (8.A):** on or off by default. Recommended: off; the
   deterministic block is defensible on its own.
5. **Corrections dataset before Phase 9 (7.E):** allowed for AGB-DE and public shop AGB
   only. Recommended: yes, behind the switch.

## Decisions taken (2026-11-17)

The user asked for all phases to be built before a final walkthrough, so each open decision
takes the recommended option:

1. **Interrupt:** both. The graph interrupts on live runs, and cached `uncertain` verdicts
   wait at review level as `awaiting_decision`.
2. **Login:** provider-agnostic OIDC. Keycloak runs in Docker Compose for development and
   tests; the production provider is configured, not coded.
3. **Object storage:** a generic S3-compatible `FileStore`, with MinIO in Compose for
   development and tests. The EU provider (Hetzner, Scaleway or OVH) is set by
   configuration.
4. **LLM summary recommendation:** off by default.
5. **Corrections dataset before Phase 9:** allowed for AGB-DE and public shop AGB, behind a
   switch.

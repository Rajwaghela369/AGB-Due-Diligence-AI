# Phase 6 Plan — Frontend

Nov 16, 2026. The lawyer's workspace on top of the Phase 5 API. Lives in `web/`, next to
`app/`.

## Goal

A lawyer reviews a whole AGB in the browser as part of daily work: reads the contract as
it was sent, sees every finding on the page, checks each one against the statute text in
one click, and says whether it was right.

**Gate:**

- a full AGB (PDF and DOCX) runs end to end in the browser, verdicts appearing live
- a page reload mid-review or after it rebuilds the workspace from stored results, with no
  new LLM calls
- a 👎 with a correction submitted in the UI arrives in Langfuse on the right step
- a Playwright test drives upload → review → open finding → feedback, in CI

## Design principles

Lawyers use legal AI widely but trust it little, and "show me the source" is what they ask
for most. So:

1. **Checkable at a glance.** Every finding names its paragraph and shows the statute
   text inline. No finding without a source.
2. **The document stays the document.** The contract renders exactly as sent; findings
   are drawn on it, never instead of it.
3. **Calm by default.** Colour is reserved for risk: red for potentially void, amber for
   needs review. Valid clauses get a faint margin mark only. Every state also has an icon
   and a label — never colour alone.
4. **The lawyer decides.** The AI suggests; the interface is built around the lawyer's
   assessment and their feedback.
5. **Daily-use ergonomics.** Keyboard first, readable typography (serif for the contract,
   sans-serif for the interface), instant reopen of past reviews, German first.
6. **Always visible:** "Keine Rechtsberatung — die Entscheidung trifft der Anwalt."

## Screens

```
Home ──► Upload ──► Review workspace (Document | Table) ──► (Export, Phase 8)
 ▲                        │
 └── reopen instantly ◄───┘
```

### Review workspace

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ⚖ ← Shop-AGB_2026.pdf                                           [Export ▾]      │
│ 🔴 4 potentially void   🟡 3 to review   🟢 17 valid   ⚪ 7 out of scope         │
├─────────────────────────────────────────────────┬────────────────────────────────┤
│ [Document] [Table]     🔍   ◀ 2 / 7 ▶   ⊖ 100% ⊕ │ FINDINGS   [Void][Review][All] │
│ ┌─────────────────────────────────────────────┐ │ ┌────────────────────────────┐ │
│ │  § 6 Haftung                                │ │ │🔴 6.1 Haftungsausschluss  │ │
│ │▌ 6.1 Unsere Haftung ist ausgeschlossen und  │◄┼─┤ § 309 Nr. 7 BGB · High     │ │
│ │▌ auf das gesetzlich zulässige Maß …        │ │ │ Pauschaler Ausschluss …    │ │
│ │▏ 6.2 Die Gewährleistung richtet sich …     │ │ └────────────────────────────┘ │
│ │▌ 6.3 Schadensersatzansprüche verjähren …    │ │ ┌────────────────────────────┐ │
│ │ ░ 7.1 Checking…                             │ │ │🟡 6.3 Verjährung           │ │
│ └─────────────────────────────────────────────┘ │ └────────────────────────────┘ │
│                                                 │  [ Activity ▸ ]                │
└─────────────────────────────────────────────────┴────────────────────────────────┘
```

### Finding detail

```
┌ 6.1 Haftungsausschluss ─────────────────────────────────── ✕ ┐
│ 🔴 Potentially void                     Confidence: High       │
│ Clause     „Unsere Haftung ist ausgeschlossen …"              │
│ Legal basis ▾ § 309 Nr. 7 BGB — statute text inline            │
│             ▸ § 307 Abs. 1 S. 2 BGB                            │
│ Reasoning  Pauschaler Ausschluss erfasst auch …                │
│ Was this assessment helpful?   👍  👎                           │
│ ▸ Technical details  (candidates · retry · trace ↗ · step votes)│
└────────────────────────────────────────────────────────────────┘
```

## Tech stack

| Concern | Choice |
| --- | --- |
| Language and build | TypeScript, React, Vite, pnpm |
| Agent stream | `@ag-ui/client` on the Phase 5 SSE endpoint (CopilotKit only if Phase 7 needs it) |
| Server data | TanStack Query |
| UI state | Zustand (selected / hovered clause, filters) |
| Routing | React Router |
| Components and styling | shadcn/ui on Radix, Tailwind CSS, lucide-react icons |
| PDF | pdf.js (`pdfjs-dist`) with our own highlight layer |
| Table | TanStack Table with virtual scrolling |
| Text | react-i18next, German first, English as a second file |
| Keyboard | react-hotkeys-hook |
| Tests and style | Vitest + Testing Library, Playwright, ESLint + Prettier, all in `make check` |

## Tasks

### 6.A · Foundation

**Goal:** a typed, tested frontend skeleton that talks to the API.

**Implementation.** `web/` with Vite, TypeScript strict, Tailwind and shadcn/ui; design
tokens for the calm palette in light and dark; API client generated from FastAPI's OpenAPI
schema so types cannot drift; AG-UI client wrapper that reconnects with `Last-Event-ID`;
i18n setup with all labels in `de.json`; `make web` (dev server) and the frontend checks
wired into `make check`.

**Done when:** the app builds, lint and unit tests pass, and a health indicator shows the
API as reachable.

### 6.B · Home and upload

**Goal:** start a review or reopen one in two clicks.

**Implementation.** Home lists reviews: document, date, risk counts, review progress, a
running review's live progress. Upload: one drop zone for PDF and DOCX, "paste text" as an
alternative, scope checkboxes (Haftung, Gewährleistung), the disclaimer. "Already reviewed"
opens the stored result. Explicit errors for wrong type, oversize, scanned PDF without
text.

**Done when:** each error state is reachable in a component test, and reopening a finished
review issues no review request.

### 6.C · Document viewer with annotations

**Goal:** the contract as sent, with every finding on the page.

**Implementation.** pdf.js renders pages lazily; a highlight layer draws each clause from
its rectangles: margin bar plus light tint, colour and icon by verdict, faint mark for
valid, nothing for out of scope. Live states from the event stream: dashed grey "queued",
soft pulse "checking", filled on verdict, "could not be checked · Retry" on failure. Step
bar during processing (Upload ✓ → Read ✓ → Split ✓ → Checking 7/31). Pasted-text documents
render as text with the same highlight model via offsets. Toolbar: text search,
`◀ n / N ▶` through findings, zoom.

**Done when:** highlights align with clause text on the splitter test PDFs at 75–200% zoom,
and clauses change state live during a review.

### 6.D · Findings panel and detail drawer

**Goal:** move between document and findings without losing your place.

**Implementation.** Findings cards in document order, filter Void / Review / All. Hover a
clause → preview popover (verdict, §, one-line reason, confidence); hover a card → its
clause highlights; click either → detail drawer pinned and the document scrolled to the
clause. Drawer: clause text, verdict with confidence as High/Medium/Low (number in
details), each cited paragraph with its statute text expandable (from
`/statutes/{chunk_id}`), German reasoning, feedback, and collapsed "Technical details"
(candidates with scores, retry, Langfuse trace link). Collapsible Activity drawer shows the
live step feed.

**Done when:** hover, click and keyboard all reach the same drawer, and the two panels stay
in sync in a component test.

### 6.E · Table view

**Goal:** the same results as a sortable grid for scanning and checking completeness.

**Implementation.** Columns: #, clause excerpt, topic, verdict, basis, confidence, status.
Sort, filter by verdict and topic, full-text search, virtual scrolling. Clicking a row
switches to the document at that clause; the same drawer opens from the table.

**Done when:** a 300-clause fixture scrolls smoothly and every filter combination is
covered by a test.

### 6.F · Feedback

**Goal:** a vote that costs one click and, on 👎, says what was wrong.

**Implementation.** 👍 / 👎 in the drawer and on each card. 👎 opens reason chips: verdict
wrong (with the correct verdict), wrong or missing § (with the correct §), reasoning
unclear, clause cut incorrectly, plus an optional comment. Votes can be changed. Posted to
`/api/v1/feedback`; the UI shows the stored vote on reload. Per-step votes appear only in
"Technical details" behind a developer setting.

**Done when:** each reason posts the expected payload, and a live run shows the score on
the right observation in Langfuse.

### 6.G · Quality and the gate

**Goal:** fit for eight hours a day, and proven end to end.

**Implementation.** Keyboard: `J`/`K` next/previous finding, `/` search, `Esc` close
(`A`/`O`/`D` reserved for Phase 7). Accessibility: focus order, contrast, labels, no
colour-only meaning. Dark mode. Performance: first page visible before any verdict, table
virtualised, PDF pages rendered on scroll. Playwright end-to-end test against the API with
fake LLM clients: upload → live review → reload → open finding → 👎 with correction.

**Done when:** the gate at the top holds.

## Out of scope

Accept / override / dismiss and review progress (Phase 7), export and summary block
(Phase 8), follow-up chat (Phase 8), login (Phase 9), mobile layout beyond tablet width.

## Open decisions

1. **Interface language:** German first with English later (recommended), or both from
   the start.
2. **AG-UI client:** plain `@ag-ui/client` (recommended) or CopilotKit components.

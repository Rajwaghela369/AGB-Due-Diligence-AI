# UI Fix Plan — findings from the Phase 6 screenshot review

Nov 17, 2026. Fixes for the problems found by capturing every page of the running app (home,
upload, workspace in document and table view, drawer, shortcuts, dark mode, tablet, 404) against
the stored `psg.pdf` review. Runs before Phase 7, because Phase 7 builds its decision UI into
the same workspace and drawer.

**Gate:**

- every bug below is fixed or explicitly deferred, confirmed by a fresh capture with
  `make ui-snapshots`
- `make check` green, with a regression test per fix
- 0 Gemini calls, except UF-D's live check (at most 2)

## Bugs

| # | Sev | Where | Bug |
| --- | --- | --- | --- |
| 1 | High | Drawer | Opens mid-screen and covers the clause it describes and most of the cards |
| 2 | High | Workspace | Selecting a card scrolls the whole page; the header and risk summary jump out of view |
| 3 | High | Table | Last column (Sicherheit) clipped; no horizontal scroll |
| 4 | High | Splitter | The letterhead/title block is split as clause #1 and judged |
| 5 | High | Cards, drawer | Titles repeat the section number: „1.1 1 Allgemeines", „3.5 3 Lieferung" |
| 6 | High | `useReview` | A finished review opens the SSE stream twice, then aborts it |
| 7 | Med | Workspace | Dokument/Tabelle tabs have no selected state |
| 8 | Med | Home | Dates in the browser locale („Nov 16, 2026"), not German |
| 9 | Med | Theme | Ignores the OS setting; not persisted across reloads |
| 10 | Med | Workspace | Risk summary is plain text, without icons or colours |
| 11 | Med | Drawer | „Sicherheit: Hohe Sicherheit": the word appears twice |
| 12 | Med | Drawer | No way to regenerate a missing reasoning („Begründung nicht verfügbar") |
| 13 | Med | Table | Thema column empty; should show „—" (topics themselves come in Phase 7.0) |
| 14 | Med | Home | An unreviewed document has no visible next step |
| 15 | Med | Tablet | The findings panel is pushed below the document, out of sight |
| 16 | Low | Upload | „Datei auswählen" looks like text; browser-blue checkboxes; disclaimer shown twice |
| 17 | Low | Theme | `transition: all` makes theme switches slow and staggered |

## Work slices

Each slice owns its files, so slices run in parallel without conflicts. The builder and the
reviewer always come from different vendors.

### UF-A · Workspace layout (bugs 1, 2, 7, 10, 15)

**Builder:** Claude Sonnet · **Reviewer:** Codex gpt-5.6-terra
**Files:** `features/review/workspace.tsx`, `features/findings/finding-drawer.tsx` (layout only),
`features/findings/findings-panel.tsx` (container only), `styles.css` (layout utilities).

- The workspace fills the viewport below the header (`h-[calc(100dvh-header)]`). The document
  and the panel scroll internally, and the page itself never scrolls. Card selection uses
  `scrollIntoView({ block: "nearest" })` on the panel's scroller only.
- The drawer **replaces the findings list inside the right column** (a master/detail panel
  with a „← Fundstellen" back button), instead of floating over the document, so the clause
  and its finding are always side by side. On wide screens (≥ 1600 px) the right column
  widens to 32rem.
- Tabs: Radix Tabs (or `aria-selected` styles) with a visible selected state.
- Risk summary: the same icon, label and colour chips as home, built from `riskStyle`.
- Tablet (< 1024 px): two tabs at the top, „Dokument" and „Fundstellen". Selecting a finding
  in the panel switches to the document and scrolls to the clause.

**Done when:** component tests assert the page's `scrollY` stays 0 on selection and that the
drawer renders inside the panel column; the snapshots show no overlap at 1440, 1024 and
820 px.

### UF-B · Clause identity and stream (bugs 4, 5, 6, 13)

**Builder:** Codex gpt-5.6-sol · **Reviewer:** Claude (orchestrator)
**Files:** `app/ingest/split.py`, `app/ingest/__init__.py` (version), `tests/test_ingest_split.py`,
`web/src/features/review/use-review.ts`, a new `web/src/features/review/clause-title.ts`, and
the table's topic cell.

- Splitter: text before the first numbered clause or `§` heading (letterhead, document title,
  „Allgemeine Geschäftsbedingungen") becomes a **preamble** with `in_scope=false` and no
  verdict. Bump `SPLITTER_VERSION`. Add a gold-set regression: no clause made only of
  title-case company or title lines.
- `clauseTitle(clause)`: number plus the heading **without its leading number**
  („3.5 · Lieferung"), used by the cards, the drawer and the table. A pure function with tests
  for `§ 6`, `6.1`, `(2)` and `a)`.
- `useReview`: open the stream only once the stored findings have loaded **and** the review
  isn't final, so a finished review makes no `/events` request. Strict-mode safe. Test it by
  counting the stream connections.
- Table topic cell: „—" when empty.

**Done when:** `psg.pdf` re-ingests with no clause #1 letterhead. The re-review runs the
changed clauses only, using the verdict cache, with **0 Gemini calls** because the clause
texts didn't change. The network log shows no `/events` request on a finished review.

### UF-C · Table, home, theme, upload (bugs 3, 8, 9, 11, 14, 16, 17)

**Builder:** Codex gpt-5.6-terra · **Reviewer:** Claude Sonnet
**Files:** `features/table/*`, `features/home/*`, `features/upload/*`, `store/ui.ts`, `app.tsx`
(theme only), the i18n `table` / `home` / `upload` / `drawer.confidence` keys.

- Table: fixed column sizes from TanStack's `size`, the excerpt column flexible, a
  horizontal scroll container, and the header sticky on both axes.
- Dates: `Intl.DateTimeFormat(i18n.language, …)` → „16.11.2026".
- Theme: the default follows `prefers-color-scheme`; the user's choice is saved in
  `localStorage` (`agb.theme`), read in a try/catch, with no flash on load (an inline script
  in `index.html`). Transitions are limited to `background-color`, `color` and
  `border-color` at 120 ms.
- Drawer: „Sicherheit: Hoch" (band label only).
- Home: an unreviewed row shows a „Prüfung starten" button (scope dialog → `reviews.start`),
  with a cost hint saying the review uses AI calls.
- Upload: a real button for „Datei auswählen", `accent-color` on the checkboxes, and one
  disclaimer (the header banner).

**Done when:** a component test per item; the snapshots in light and dark match.

### UF-D · Regenerate a missing reasoning (bug 12)

**Builder:** Claude Sonnet · **Reviewer:** Codex gpt-5.6-sol
**Files:** `app/agent/run.py` (an `explain_only`), `app/review/service.py` and `runner.py`
(one method), `app/api/routes/reviews.py`, a new
`web/src/features/findings/regenerate-reason.tsx`, and the drawer (one import).

- `POST /reviews/{id}/clauses/{pos}/explain` re-runs only the explanation step for a
  void verdict with `reason_unavailable`: **1 Gemini call**, retried on 429 and 5xx, and
  through the limiter. Stored on the verdict row and emitted as a `STATE_DELTA`. 409 if the
  verdict is not void or already has a reason.
- The drawer shows „Begründung erzeugen" in place of the unavailable notice.

**Done when:** fake-model tests for success, 409 and provider failure; one live check on
psg.pdf clause 3.5 (at most 2 Gemini calls, counted in the API logs).

### UF-E · Screenshot regression (makes the gate repeatable)

**Builder:** Claude Haiku · **Reviewer:** the orchestrator
Commit the capture script as `web/e2e/snapshots.spec.ts` with `make ui-snapshots`: pages ×
{1440, 1024, 820} × {light, dark} against `build_test_app()` (fake models), plus an assertion
of no horizontal overflow and no console errors. Use Playwright `toHaveScreenshot` with a
small threshold so layout regressions fail CI.

## Schedule

```
Wave 1  UF-A (Claude Sonnet) ║ UF-B (Codex sol) ║ UF-C (Codex terra)      → cross-review, merge
Wave 2  UF-D (Claude Sonnet) ║ UF-E (Claude Haiku)                       → cross-review, merge
Gate    orchestrator: make ui-snapshots + live capture of psg.pdf (0–2 Gemini calls)
```

# Export templates (Phase 8, 8.B)

`app.review.export` renders the DD memo from the files in this folder
(`export.template_dir` in `configs/app.yaml`). The export builder (Phase 8 Wave 1) adds
them; until then the folder holds only this note.

## What goes here

- **`memo.docx`**: the Word template, made in Word or LibreOffice, read with `python-docx`.
  It carries the styles and page setup only; the content is written by code, so the
  template never contains contract text. It defines:
  - the paragraph styles `Title`, `Heading 1`, `Heading 2`, `Normal`, `Quote` (clause
    text) and `Disclaimer`, plus a table style `AGB Findings` for the findings table;
  - A4 portrait, German hyphenation, a footer with the page number and
    `agent_version`;
  - no watermark: the draft watermark „Entwurf“ (`app.domain.DRAFT_WATERMARK`) is added by
    code to the header of every section, only for a draft export.
- **`memo.md.j2`** (optional): if the Markdown layout is kept as a template rather than in
  code. Plain `str.format`-style placeholders or Jinja2 only if it is already a dependency;
  no new dependency without asking.

## The memo, in order

1. **Cover**: document filename, review date, exporter (`X-Reviewer`, „Unbekannt“ when
   absent, marked „nicht angemeldet“ before Phase 9), the reviewers who decided,
   `agent_version`, and `app.domain.EXPORT_DISCLAIMER`.
2. **Summary**: `ReviewSummary`: counts, void clauses grouped by §, top risks,
   recommendation (marked as AI-generated when `recommendation_from_llm`).
3. **Decided findings**: one table row per finding with a verdict: clause number and
   excerpt, decision, verdict in force, §, reasoning (the decision's `reason_de` when set,
   else the verdict's), note, decided by, decided at.
4. **Appendix**: the full text of every cited statute chunk, in § order.

Golden files for all three formats live with the tests (`tests/golden/export/`), not here.
DOCX output must be byte-stable for a fixed input: fix the zip timestamps and core
properties (created/modified = the review's `finished_at`).

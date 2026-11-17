// Phase 8 gate: from a raw document to a DD-ready export, built only from the lawyer's
// decisions. The final export is refused while void or uncertain findings are undecided; once
// the lawyer decides them in the UI, DOCX, Markdown and CSV download with date, reviewer and
// agent_version, without „Entwurf“, and with the overridden verdict in force. A draft carries
// the watermark. The rewrite chat streams the fake model's answer. No Gemini request is made.
import { expect, test, type Download, type Page, type TestInfo } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const API = "http://127.0.0.1:8010/api/v1";
const PSG = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../data/gold/splitter/psg.pdf",
);
const REVIEWER = "E2E Anwältin";
const AGENT_VERSION = "agent-e2e";

interface Finding {
  position: number;
  status: string;
  decision: { action: string } | null;
  verdict: { verdict: string } | null;
}

const reviewIdOf = (page: Page) => new URL(page.url()).pathname.split("/").pop() as string;

async function findings(page: Page, reviewId: string): Promise<Finding[]> {
  const response = await page.request.get(`${API}/reviews/${reviewId}/findings`);
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { findings: Finding[] }).findings;
}

const needsDecision = (finding: Finding) =>
  finding.decision === null &&
  (finding.verdict?.verdict === "potentially_void" || finding.verdict?.verdict === "uncertain");

/** Today in the export's own zone (Europe/Berlin), as the memo prints it and as ISO. */
function berlinToday(): { german: string; iso: string } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Berlin",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    })
      .formatToParts(new Date())
      .map((part) => [part.type, part.value]),
  );
  return {
    german: `${parts.day}.${parts.month}.${parts.year}`,
    iso: `${parts.year}-${parts.month}-${parts.day}`,
  };
}

/** RFC 4180 with `;`: quoted fields may hold separators, quotes and line breaks. */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i]!;
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') {
        field += '"';
        i++;
      } else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ";") {
      row.push(field);
      field = "";
    } else if (char === "\r" && text[i + 1] === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      i++;
    } else field += char;
  }
  if (field !== "" || row.length > 0) rows.push([...row, field]);
  return rows;
}

async function save(download: Download, testInfo: TestInfo): Promise<string> {
  const target = testInfo.outputPath(download.suggestedFilename());
  await download.saveAs(target);
  return target;
}

/** Text of the DOCX parts that matter: body, and every header (where the watermark sits). */
function docxParts(file: string): { body: string; headers: string } {
  const unzip = (part: string) => execFileSync("unzip", ["-p", file, part]).toString("utf-8");
  const names = execFileSync("unzip", ["-Z1", file]).toString("utf-8").split("\n");
  const headers = names.filter((name) => /^word\/header\d*\.xml$/.test(name));
  return { body: unzip("word/document.xml"), headers: headers.map(unzip).join("\n") };
}

async function openExport(page: Page) {
  await page.getByTestId("review-summary").getByRole("button", { name: "Exportieren" }).click();
  return page.getByRole("dialog");
}

async function exportFile(
  page: Page,
  testInfo: TestInfo,
  format: "DOCX" | "MD" | "CSV",
  draft: boolean,
): Promise<{ file: string; name: string }> {
  const dialog = await openExport(page);
  await dialog.getByRole("radio", { name: format }).check();
  const checkbox = dialog.getByRole("checkbox");
  if (draft) await checkbox.check();
  else await expect(checkbox).not.toBeChecked();
  const downloading = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Exportieren" }).click();
  const download = await downloading;
  await expect(dialog).toBeHidden();
  return { file: await save(download, testInfo), name: download.suggestedFilename() };
}

test("a raw document becomes a DD-ready export built from the lawyer's decisions", async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000);
  const today = berlinToday();
  await page.addInitScript((name) => localStorage.setItem("agb.reviewer", name), REVIEWER);
  await page.goto("/upload");
  // A distinct trailing comment gives this test its own review (one review per document hash).
  await page.locator('input[type="file"]').setInputFiles({
    name: "psg-export.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.concat([readFileSync(PSG), Buffer.from("\n%export-e2e\n")]),
  });
  await page.getByRole("button", { name: "Prüfung starten" }).click();
  await page.waitForURL(/\/reviews\/[0-9a-f-]+/);
  const reviewId = reviewIdOf(page);

  await expect
    .poll(
      async () =>
        (
          (await (await page.request.get(`${API}/reviews/${reviewId}`)).json()) as {
            review: { status: string };
          }
        ).review.status,
      { timeout: 30_000 },
    )
    .toBe("awaiting_decisions");
  const blocking = (await findings(page, reviewId)).filter(needsDecision);
  expect(blocking.length).toBeGreaterThan(1);

  // 1. The final export is refused and names every undecided finding.
  await expect(page.getByTestId("review-summary")).toBeVisible();
  const dialog = await openExport(page);
  // With open findings the dialog proposes a draft; ask for the final export on purpose.
  await expect(dialog.getByRole("checkbox")).toBeChecked();
  await dialog.getByRole("checkbox").uncheck();
  await dialog.getByRole("button", { name: "Exportieren" }).click();
  const alert = dialog.getByRole("alert");
  await expect(alert).toContainText("finale Export ist noch blockiert");
  await expect(alert.getByRole("listitem")).toHaveCount(blocking.length);

  // 2. A draft is allowed and carries the watermark: DOCX in the page header, Markdown in
  // its first line, and the filename says so.
  await dialog.getByRole("button", { name: "Stattdessen als Entwurf exportieren" }).click();
  await expect(dialog.getByRole("checkbox")).toBeChecked();
  const draftDownload = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Exportieren" }).last().click();
  const draftDocx = await save(await draftDownload, testInfo);
  expect(draftDocx).toContain("_Entwurf");
  const parts = docxParts(draftDocx);
  expect(parts.headers).toContain('string="Entwurf"');
  const draftMd = await exportFile(page, testInfo, "MD", true);
  expect(draftMd.name).toContain("_Entwurf");
  expect(readFileSync(draftMd.file, "utf-8").split("\n")[0]).toContain("ENTWURF");

  // 3. The lawyer decides every blocking finding in the UI: the first is overridden to valid,
  // the others are accepted.
  const [overridden, ...accepted] = blocking;
  await page
    .getByTestId(`finding-card-${overridden!.position}`)
    .getByRole("button")
    .first()
    .click();
  await page.keyboard.press("o");
  const form = page.getByTestId("override-form");
  await form.getByRole("radio", { name: "Wirksam", exact: true }).check();
  await page.getByLabel("Notiz (optional)").fill("Nach Prüfung unbedenklich.");
  await form.getByRole("button", { name: "Überschreiben bestätigen" }).click();
  await expect(
    page.getByTestId(`finding-card-${overridden!.position}`).getByTestId("decision-status"),
  ).toHaveText("Überschrieben");
  await page.keyboard.press("Escape");
  for (const finding of accepted) {
    const card = page.getByTestId(`finding-card-${finding.position}`);
    await card.getByRole("button").first().click();
    await page.keyboard.press("a");
    await expect(card.getByTestId("decision-status")).toHaveText("Akzeptiert");
    await page.keyboard.press("Escape");
  }
  await expect
    .poll(async () => (await findings(page, reviewId)).filter(needsDecision).length)
    .toBe(0);

  // 4. The final exports: dated, attributed, versioned, no „Entwurf“, the override in force.
  const stored = await findings(page, reviewId);
  const overriddenPosition = overridden!.position;
  const md = await exportFile(page, testInfo, "MD", false);
  const docx = await exportFile(page, testInfo, "DOCX", false);
  const csv = await exportFile(page, testInfo, "CSV", false);
  for (const { name } of [md, docx, csv]) {
    expect(name).toContain(`_${today.iso}.`);
    expect(name).not.toContain("Entwurf");
  }

  const memo = readFileSync(md.file, "utf-8");
  expect(memo).toContain(`**Prüfdatum:** ${today.german}`);
  expect(memo).toContain(`**Exportiert am:** ${today.german}`);
  expect(memo).toContain(`**Exportiert von:** ${REVIEWER}`);
  expect(memo).toContain(`**Agent-Version:** \`${AGENT_VERSION}\``);
  expect(memo).toContain("Maßgeblich sind allein die Entscheidungen der prüfenden Person");
  expect(memo).not.toMatch(/entwurf/i);
  expect(memo).toContain("Nach Prüfung unbedenklich.");

  const docxText = docxParts(docx.file);
  const plain = docxText.body.replace(/<[^>]+>/g, "");
  expect(plain).toContain(today.german);
  expect(plain).toContain(REVIEWER);
  expect(plain).toContain(AGENT_VERSION);
  expect(plain).toContain("Nach Prüfung unbedenklich.");
  expect(docxText.body + docxText.headers).not.toMatch(/entwurf/i);

  const raw = readFileSync(csv.file);
  expect([...raw.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
  const rows = parseCsv(raw.toString("utf-8").replace(/^\uFEFF/, ""));
  const header = rows[0]!;
  const col = (row: string[], name: string) => row[header.indexOf(name)];
  const byPosition = new Map(rows.slice(1).map((row) => [col(row, "position"), row]));
  const decided = rows.slice(1).filter((row) => col(row, "entscheidung") !== "offen");
  expect(decided.length).toBe(blocking.length);
  for (const row of decided) {
    expect(col(row, "entschieden_von")).toBe(REVIEWER);
    expect(col(row, "entschieden_am")).toMatch(/^\d{4}-\d{2}-\d{2}T.*Z$/);
  }
  for (const row of rows.slice(1)) {
    expect(col(row, "agent_version")).toBe(AGENT_VERSION);
    expect(col(row, "entwurf")).toBe("nein");
  }
  const override = byPosition.get(String(overriddenPosition))!;
  expect(col(override, "entscheidung")).toBe("geändert");
  expect(col(override, "ergebnis")).toBe("valid");
  expect(col(override, "notiz")).toBe("Nach Prüfung unbedenklich.");
  expect(col(override, "modell_ergebnis")).toBe(overridden!.verdict!.verdict);
  expect(stored.length).toBeGreaterThanOrEqual(rows.length - 1);

  // 5. The rewrite chat streams the fake model's answer with disclaimer and diff.
  const target =
    blocking.find((finding) => finding.verdict?.verdict === "potentially_void") ?? accepted[0]!;
  await page.getByTestId(`finding-card-${target.position}`).getByRole("button").first().click();
  const chat = page.getByTestId("rewrite-chat");
  await chat.getByRole("button", { name: "Umformulieren" }).click();
  await chat.getByRole("button", { name: /rechtskonform um$/ }).click();
  await expect(chat.getByTestId("chat-answer-text")).toContainText("Ein möglicher Vorschlag");
  await expect(chat.getByTestId("chat-disclaimer")).toBeVisible();
  await expect(chat.getByTestId("chat-diff")).toBeVisible();
  await expect(chat.locator("ins, del").first()).toBeVisible();
});

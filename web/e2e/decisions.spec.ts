// Phase 7 gate: an interrupted finding remains reviewable across a reload, and the final
// decision closes the review. The app uses the offline fake API, never a Gemini request.
import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const API = "http://127.0.0.1:8010/api/v1";
const PSG = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../data/gold/splitter/psg.pdf",
);

interface Finding {
  position: number;
  status: string;
  decision: { action: string; corrected_verdict: string | null; note: string } | null;
  verdict: { verdict: string } | null;
}

const reviewIdOf = (page: Page) => new URL(page.url()).pathname.split("/").pop() as string;

async function findings(page: Page, reviewId: string): Promise<Finding[]> {
  const response = await page.request.get(`${API}/reviews/${reviewId}/findings`);
  expect(response.ok()).toBe(true);
  return ((await response.json()) as { findings: Finding[] }).findings;
}

async function review(page: Page, reviewId: string) {
  const response = await page.request.get(`${API}/reviews/${reviewId}`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as { review: { status: string; counts: Record<string, number> } };
}

test("reload keeps an override and the final acceptance finishes the review", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("agb.reviewer", "E2E Anwältin"));
  await page.goto("/upload");
  await page.locator('input[type="file"]').setInputFiles(PSG);
  await page.getByRole("button", { name: "Prüfung starten" }).click();
  await page.waitForURL(/\/reviews\/[0-9a-f-]+/);
  const reviewId = reviewIdOf(page);

  await expect
    .poll(async () => (await review(page, reviewId)).review.status, { timeout: 30_000 })
    .toBe("awaiting_decisions");
  const interrupted = (await findings(page, reviewId)).find(
    (finding) => finding.status === "awaiting_decision",
  )!;
  expect(interrupted.verdict?.verdict).toBe("uncertain");

  await page
    .getByTestId(`finding-card-${interrupted.position}`)
    .getByRole("button")
    .first()
    .click();
  await expect(page.getByTestId("finding-drawer")).toBeVisible();
  await page.keyboard.press("o");
  const form = page.getByTestId("override-form");
  await expect(form).toBeVisible();
  await form.getByRole("radio", { name: "Potenziell unwirksam" }).check();
  await page.getByLabel("Notiz (optional)").fill("§ 309 Nr. 7 BGB ist einschlägig.");
  await form.getByRole("button", { name: "Überschreiben bestätigen" }).click();
  await expect(page.getByTestId("decision-status")).toHaveText("Überschrieben");

  await page.reload();
  const overriddenCard = page.getByTestId(`finding-card-${interrupted.position}`);
  await expect(overriddenCard.getByTestId("decision-status")).toHaveText("Überschrieben");
  await overriddenCard.getByRole("button").first().click();
  await expect(page.getByTestId("current-decision")).toContainText("Potenziell unwirksam");
  await expect(page.getByTestId("current-decision")).toContainText(
    "§ 309 Nr. 7 BGB ist einschlägig.",
  );
  await expect(page.getByTestId("decision-progress")).toContainText("1 von 2 entschieden");
  const auditList = page.getByTestId("audit-list");
  await expect(auditList.getByRole("heading", { name: "Verlauf" })).toBeVisible();
  await expect(auditList).toContainText("Überschrieben");

  const audit = await page.request.get(
    `${API}/reviews/${reviewId}/audit?position=${interrupted.position}`,
  );
  expect(audit.ok()).toBe(true);
  expect((await audit.json()).events).toEqual([
    expect.objectContaining({
      action: "override",
      from_verdict: "uncertain",
      to_verdict: "potentially_void",
      note: "§ 309 Nr. 7 BGB ist einschlägig.",
    }),
  ]);

  const voidFinding = (await findings(page, reviewId)).find(
    (finding) =>
      finding.position !== interrupted.position && finding.verdict?.verdict === "potentially_void",
  )!;
  await page
    .getByTestId(`finding-card-${voidFinding.position}`)
    .getByRole("button")
    .first()
    .click();
  await page.keyboard.press("a");
  await expect(page.getByTestId("decision-status")).toHaveText("Akzeptiert");
  await page.reload();

  const stored = await review(page, reviewId);
  expect(stored.review.status).toBe("finished");
  expect(stored.review.counts.awaiting_decision ?? 0).toBe(0);
  expect(stored.review.counts.open_review ?? 0).toBe(0);
  expect(stored.review.counts.open_void ?? 0).toBe(0);
  await expect(page.getByTestId("decision-progress")).toContainText("2 von 2 entschieden");

  await page.goto("/");
  const agreement = page.getByTestId("agreement");
  await expect(agreement).toContainText("Entschieden: 2");
  await expect(agreement.getByTestId("agreement-rate")).toHaveText("50 %");
  const metric = await page.request.get(`${API}/metrics/agreement`);
  expect(metric.ok()).toBe(true);
  expect((await metric.json()).agreement).toMatchObject({
    agent_version: "agent-e2e",
    decided: 2,
    kept: 1,
    overridden: 1,
    dismissed: 0,
    rate: 0.5,
  });
});

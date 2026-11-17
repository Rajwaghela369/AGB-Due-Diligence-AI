// The Phase 6 gate, in a real browser: upload -> live review -> reload -> open a finding ->
// 👎 with a correction. The API is the real one with fake model clients, so the run is
// deterministic and offline. The tests share one server and one review, so they run in order.
import AxeBuilder from "@axe-core/playwright";
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
  verdict: { verdict: string } | null;
  clause: { number?: string; text: string; rects?: Rect[] };
}
interface Rect {
  page: number;
  x0: number;
  x1: number;
  top: number;
  bottom: number;
}

async function judgeCalls(page: Page): Promise<number> {
  const response = await page.request.get(`${API}/__test__/judge-calls`);
  return ((await response.json()) as { calls: number }).calls;
}

async function storedFindings(page: Page, reviewId: string): Promise<Finding[]> {
  const response = await page.request.get(`${API}/reviews/${reviewId}/findings`);
  return ((await response.json()) as { findings: Finding[] }).findings;
}

/** The slice of the viewer's pdf.js wrapper the alignment check uses. */
interface PdfLike {
  getPage(n: number): Promise<{
    view: number[];
    textItems(): Promise<
      Array<{ str: string; transform: number[]; width: number; height: number }>
    >;
  }>;
}

const reviewIdOf = (page: Page) => new URL(page.url()).pathname.split("/").pop() as string;

test.describe.configure({ mode: "serial" });

let reviewId = "";
let voidPosition = -1;

test("upload, live review, reload without new judge calls, open a finding, 👎 with correction", async ({
  page,
}) => {
  await page.goto("/upload");
  await page.locator('input[type="file"]').setInputFiles(PSG);
  await page.getByRole("button", { name: "Prüfung starten" }).click();
  await page.waitForURL(/\/reviews\/[0-9a-f-]+/);
  reviewId = reviewIdOf(page);

  // The first page shows before any verdict exists.
  const firstPage = page.getByRole("region", { name: /^Seite 1 von/ });
  await expect(firstPage).toBeVisible();
  await expect(page.getByTestId("risk-summary")).toBeVisible();

  // Live: clause states change while the review runs; the first step bar is visible.
  await expect(page.getByRole("list", { name: "Fortschritt der Prüfung" })).toBeVisible();
  await expect
    .poll(
      async () => (await storedFindings(page, reviewId)).filter((f) => f.status === "done").length,
    )
    .toBeGreaterThan(0);

  // Reload in the middle of the review: the workspace is rebuilt and the run carries on.
  await page.reload();
  await expect(page.getByTestId("risk-summary")).toBeVisible();

  await expect
    .poll(
      async () =>
        (await storedFindings(page, reviewId)).filter((f) =>
          ["queued", "checking"].includes(f.status),
        ).length,
      { timeout: 30_000 },
    )
    .toBe(0);
  await expect(page.getByText("1 potenziell unwirksam")).toBeVisible();
  await expect(page.getByText("1 zu prüfen")).toBeVisible();
  const findings = await storedFindings(page, reviewId);
  voidPosition = findings.find((f) => f.verdict?.verdict === "potentially_void")!.position;

  // Reload after it finished: rebuilt from stored results, no judge call is made.
  const calls = await judgeCalls(page);
  expect(calls).toBe(findings.filter((f) => f.status === "done").length);
  await page.reload();
  await expect(page.getByText("1 potenziell unwirksam")).toBeVisible();
  await expect(page.getByTestId(`finding-card-${voidPosition}`)).toBeVisible();
  expect(await judgeCalls(page)).toBe(calls);

  // Open the finding.
  await page.getByTestId(`finding-card-${voidPosition}`).getByRole("button").first().click();
  const drawer = page.getByTestId("finding-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText("Testbegründung zur Klausel.")).toBeVisible();

  // 👎 with a correction.
  await drawer.getByRole("button", { name: "Nicht hilfreich" }).click();
  await drawer.getByText("Urteil falsch").click();
  await drawer.getByLabel("Richtiges Urteil").selectOption("valid");
  await drawer.getByLabel("Kommentar (optional)").fill("Klausel ist hier ausnahmsweise wirksam.");
  await drawer.getByRole("button", { name: "Senden" }).click();

  // The fake Langfuse recorded a score on the judge observation.
  await expect
    .poll(async () => {
      const response = await page.request.get(`${API}/__test__/scores`);
      return ((await response.json()) as { scores: Array<Record<string, unknown>> }).scores;
    })
    .toEqual([
      expect.objectContaining({
        name: "feedback.judge",
        value: -1,
        observation_id: expect.stringMatching(/^judge-/),
        comment: expect.stringContaining("Korrigiertes Urteil: valid"),
      }),
    ]);

  // Voting never triggers a new judgement.
  expect(await judgeCalls(page)).toBe(calls);
});

test("keyboard: J/K step through findings, / focuses search, ? lists the shortcuts, Esc closes", async ({
  page,
}) => {
  await page.goto(`/reviews/${reviewId}`);
  await expect(page.getByTestId("risk-summary")).toBeVisible();
  await expect(page.getByTestId("finding-position")).toHaveText(/^– \/ \d+/);

  await page.keyboard.press("j");
  await expect(page.getByTestId("finding-position")).toHaveText(/^1 \/ \d+/);
  await page.keyboard.press("j");
  await expect(page.getByTestId("finding-position")).toHaveText(/^2 \/ \d+/);
  await page.keyboard.press("k");
  await expect(page.getByTestId("finding-position")).toHaveText(/^1 \/ \d+/);

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("finding-drawer")).toBeHidden();

  await page.keyboard.press("/");
  const search = page.getByRole("searchbox");
  await expect(search).toBeFocused();
  // Typing a hotkey letter into the search box does not move the selection.
  await page.keyboard.type("jk");
  await expect(search).toHaveValue("jk");
  await expect(page.getByTestId("finding-position")).toHaveText(/^– \/ \d+/);
  await page.keyboard.press("Escape");
  await expect(search).toHaveValue("");

  await search.blur();
  await page.keyboard.press("?");
  const help = page.getByRole("dialog", { name: "Tastenkürzel" });
  await expect(help).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(help).toBeHidden();
});

for (const zoomLabel of ["Verkleinern", "Vergrößern"]) {
  test(`viewer alignment at ${zoomLabel === "Verkleinern" ? "75" : "200"} %: a highlight overlaps the text of its first line`, async ({
    page,
  }) => {
    await page.goto(`/reviews/${reviewId}`);
    const target = (await storedFindings(page, reviewId)).find(
      (f) => f.clause.rects && f.clause.rects.length > 0 && f.clause.rects[0]!.page === 1,
    )!;
    const zoom = page.getByRole("button", { name: zoomLabel });
    await expect(page.getByRole("region", { name: /^Seite 1 von/ })).toBeVisible();
    while (await zoom.isEnabled()) await zoom.click();
    await expect(zoom).toBeDisabled();

    const line = page.locator(`[data-clause-line="${target.position}"]`).first();
    await expect(line).toBeAttached();
    const result = await page.evaluate(
      async ({ position, fileUrl }) => {
        const frame = document.querySelector('[data-page="1"]') as HTMLElement;
        const box = (
          document.querySelector(`[data-clause-line="${position}"]`) as HTMLElement
        ).getBoundingClientRect();
        const origin = frame.getBoundingClientRect();
        const bytes = await (await fetch(fileUrl)).arrayBuffer();
        const modulePath = "/src/features/viewer/pdf.ts";
        const { openPdf } = (await import(/* @vite-ignore */ modulePath)) as {
          openPdf: (data: ArrayBuffer) => Promise<PdfLike>;
        };
        const pdf = await openPdf(bytes);
        const first = await pdf.getPage(1);
        const [, , width, height] = first.view;
        const scale = (frame.getBoundingClientRect().width / (width as number)) as number;
        const items = await first.textItems();
        // Text items drawn as PDF points, as boxes relative to the page frame in CSS px.
        const boxes = items
          .filter((item) => item.str.trim().length > 0)
          .map((item) => {
            const [, , , , e, f] = item.transform as number[];
            return {
              text: item.str,
              left: (e as number) * scale,
              top: ((height as number) - (f as number) - item.height) * scale,
              right: ((e as number) + item.width) * scale,
              bottom: ((height as number) - (f as number)) * scale,
            };
          });
        const hl = {
          left: box.left - origin.left,
          top: box.top - origin.top,
          right: box.right - origin.left,
          bottom: box.bottom - origin.top,
        };
        const overlapping = boxes.filter(
          (b) =>
            Math.min(b.right, hl.right) - Math.max(b.left, hl.left) > 1 &&
            Math.min(b.bottom, hl.bottom) - Math.max(b.top, hl.top) > 1,
        );
        return { hl, scale, overlapping: overlapping.map((b) => b.text) };
      },
      {
        position: target.position,
        fileUrl: `/api/v1/documents/${(await (await page.request.get(`${API}/reviews/${reviewId}`)).json()).review.document_id}/file`,
      },
    );
    expect(result.overlapping.length).toBeGreaterThan(0);
    // The overlapping text is part of the clause.
    const clauseText = target.clause.text.replace(/\s+/g, " ");
    expect(
      result.overlapping.some((text) => clauseText.includes(text.trim().replace(/\s+/g, " "))),
    ).toBe(true);
  });
}

for (const theme of ["light", "dark"] as const) {
  test(`accessibility (${theme}): no serious or critical axe violations on home, upload and workspace`, async ({
    page,
  }) => {
    await page.goto("/");
    await page.evaluate((mode) => {
      document.documentElement.classList.toggle("dark", mode === "dark");
    }, theme);
    for (const route of ["/", "/upload", `/reviews/${reviewId}`]) {
      await page.goto(route);
      // No colour transitions, so axe never samples a half-faded theme switch.
      await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important }" });
      await page.evaluate((mode) => {
        document.documentElement.classList.toggle("dark", mode === "dark");
      }, theme);
      await expect(page.locator("main")).toBeVisible();
      if (route.startsWith("/reviews")) {
        await expect(page.getByTestId("risk-summary")).toBeVisible();
        await expect(page.getByRole("region", { name: /^Seite 1 von/ })).toBeVisible();
        await page.getByTestId(`finding-card-${voidPosition}`).getByRole("button").first().click();
        await expect(page.getByTestId("finding-drawer")).toBeVisible();
      } else if (route === "/") {
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      }
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"])
        .analyze();
      const blocking = results.violations.filter((v) =>
        ["serious", "critical"].includes(v.impact ?? ""),
      );
      expect(
        blocking.map((v) => ({
          id: v.id,
          impact: v.impact,
          nodes: v.nodes
            .slice(0, 3)
            .map((n) => `${n.target.join(" ")} :: ${n.any[0]?.message ?? ""}`),
        })),
        `${route} (${theme})`,
      ).toEqual([]);
    }
  });
}

for (const [width, height] of [
  [1440, 900],
  [820, 1180],
] as const) {
  test(`page scrollY stays 0 when a finding is selected (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await page.goto(`/reviews/${reviewId}`);
    await expect(page.getByTestId("risk-summary")).toBeVisible();
    if (width < 1024) await page.getByRole("tab", { name: "Fundstellen" }).click();
    const last = page.getByTestId("finding-card-30");
    await last.scrollIntoViewIfNeeded();
    await last.getByRole("button").first().click();
    await page.waitForTimeout(500);
    const metrics = await page.evaluate(() => ({
      y: window.scrollY,
      scroll: document.documentElement.scrollHeight,
      inner: window.innerHeight,
    }));
    expect(metrics.y).toBe(0);
    expect(metrics.scroll).toBeLessThanOrEqual(metrics.inner);
  });
}

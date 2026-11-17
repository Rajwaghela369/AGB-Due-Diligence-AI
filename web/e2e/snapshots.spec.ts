// Screenshot regression for the layout: every page x {1440, 1024, 820} px x {light, dark}, against
// the fake-model API (app/api/testing.py), so the run is offline and spends no Gemini calls.
//
// Determinism comes from four things: the fonts are fixed to two families every Mac has, all
// transitions and animations are switched off, the only clock-dependent text (the upload date on
// the home page) is masked, and the review is finished before anything is captured, so no pixel
// depends on how far a run has got. Baselines are per platform (Playwright adds the OS suffix),
// so they are recorded on the machine that gates the change.
//
// The tests share one uploaded review, so they run in order in one worker.
import { expect, test, type Page } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const API = "http://127.0.0.1:8010/api/v1";
const PSG = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../data/gold/splitter/psg.pdf",
);

const WIDTHS = [1440, 1024, 820] as const;
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

// Arial and Georgia ship with macOS, so the captures do not depend on the installed Inter.
const FIXED_FONTS = `
:root { --font-sans: Arial, Helvetica, sans-serif !important; --font-contract: Georgia, serif !important; }
html, body { font-family: Arial, Helvetica, sans-serif !important; }
*, *::before, *::after {
  transition: none !important;
  animation: none !important;
  caret-color: transparent !important;
}
`;

/** Upload date of the document, the only text that changes with the clock. */
const CLOCK_TEXT = (page: Page) => page.locator("li span.mt-1:has(svg.lucide-calendar-days)");

let reviewId = "";

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ browser }) => {
  const page = await browser.newPage({ locale: "de-DE" });
  await page.goto("/upload");
  await page.locator('input[type="file"]').setInputFiles(PSG);
  await page.getByRole("button", { name: "Prüfung starten" }).click();
  await page.waitForURL(/\/reviews\/[0-9a-f-]+/);
  reviewId = new URL(page.url()).pathname.split("/").pop() as string;
  // The judge is a keyword fake with a short delay: wait for the whole run to finish.
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`${API}/reviews/${reviewId}`);
        return ((await response.json()) as { review: { status: string } }).review.status;
      },
      { timeout: 120_000 },
    )
    .toBe("finished");
  await page.close();
});

interface Route {
  name: string;
  open: (page: Page) => Promise<void>;
}

const ROUTES: Route[] = [
  {
    name: "home",
    open: async (page) => {
      await page.goto("/");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    },
  },
  {
    name: "upload",
    open: async (page) => {
      await page.goto("/upload");
      await expect(page.getByTestId("upload-page")).toBeVisible();
    },
  },
  {
    name: "workspace",
    open: async (page) => {
      await page.goto(`/reviews/${reviewId}`);
      await expect(page.getByTestId("risk-summary")).toBeVisible();
      await expect(page.getByRole("region", { name: /^Seite 1 von/ })).toBeVisible();
    },
  },
  {
    name: "workspace-table",
    open: async (page) => {
      await page.goto(`/reviews/${reviewId}`);
      await expect(page.getByTestId("risk-summary")).toBeVisible();
      const tab = page.getByRole("tab", { name: "Tabelle" });
      await tab.click();
      await expect(tab).toHaveAttribute("aria-selected", "true");
    },
  },
  {
    name: "not-found",
    open: async (page) => {
      await page.goto("/does-not-exist");
      await expect(page).toHaveURL(/\/404$/);
      await expect(page.getByRole("main")).toBeVisible();
    },
  },
];

for (const route of ROUTES) {
  for (const width of WIDTHS) {
    for (const theme of THEMES) {
      test(`${route.name} at ${width} px, ${theme}`, async ({ page }) => {
        const problems: string[] = [];
        page.on("console", (message) => {
          if (message.type() === "error") problems.push(`console.error: ${message.text()}`);
        });
        page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));

        await page.setViewportSize({ width, height: 900 });
        await page.emulateMedia({ colorScheme: theme });
        await page.addInitScript((mode: Theme) => {
          try {
            window.localStorage.setItem("agb.theme", mode);
          } catch {
            // Storage unavailable: the OS preference (emulated above) still picks the theme.
          }
        }, theme);

        await route.open(page);
        await page.addStyleTag({ content: FIXED_FONTS });
        await page.evaluate(() => document.fonts.ready);

        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow, "horizontal overflow in px").toBeLessThanOrEqual(0);

        await expect(page).toHaveScreenshot(`${route.name}-${width}-${theme}.png`, {
          fullPage: true,
          animations: "disabled",
          caret: "hide",
          mask: route.name === "home" ? [CLOCK_TEXT(page)] : [],
        });

        expect(problems, "console errors").toEqual([]);
      });
    }
  }
}

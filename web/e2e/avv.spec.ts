import { expect, test } from "@playwright/test";

test("selects AVV at upload and opens the review workspace", async ({ page }) => {
  await page.route("**/api/v1/documents", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({
      json: {
        document: {
          document_id: "avv-document",
          document_type: "avv",
          filename: "avv.pdf",
          parser_version: "p1",
          source_type: "pdf",
          splitter_version: "s1",
          text_hash: "text-hash",
        },
        clauses: [],
        reused: false,
        latest_review: null,
      },
    });
  });
  await page.route("**/api/v1/reviews", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({
      json: {
        review: {
          review_id: "avv-review",
          document_id: "avv-document",
          document_type: "avv",
          agent_version: "avv-v1",
          scope: ["liability"],
          status: "finished",
          counts: { total: 0, done: 0, failed: 0 },
        },
      },
    });
  });

  await page.goto("/upload");
  await expect(page.getByTestId("document-type")).toBeVisible();
  await page.locator('input[name="document-type"][value="avv"]').check();
  await expect(page.locator('input[name="document-type"][value="avv"]')).toBeChecked();
  await page.locator('input[type="file"]').setInputFiles({
    name: "avv.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF"),
  });
  await page.getByRole("button", { name: "Prüfung starten" }).click();
  await page.waitForURL("**/reviews/avv-review");
});

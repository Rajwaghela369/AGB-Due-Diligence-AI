import { fireEvent, render, screen, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { ReviewSummary } from "../../api/types.gen";
import "../../i18n";
import { server } from "../../test/server";
import { ORIGIN, installOriginFetch } from "../home/test-utils";
import { SummaryBlock } from "./summary-block";

installOriginFetch();

const summary = {
  review_id: "review-1",
  document_id: "document-1",
  filename: "AGB.pdf",
  agent_version: "v1",
  review_status: "finished",
  draft: true,
  counts: { potentially_void: 2, uncertain: 1, valid: 4 },
  recommendation: "Zwei Klauseln sollten überarbeitet werden.",
  decision_set_hash: "hash",
  generated_at: "2026-11-17T10:00:00Z",
  top_risks: [
    {
      position: 3,
      number: "6.1",
      excerpt: "Ein Haftungsausschluss geht zu weit.",
      confidence: 0.91,
      finding_status: "open",
      model_verdict: "potentially_void",
      verdict: "potentially_void",
    },
  ],
  void_groups: [{ key: "bgb-309", label: "§ 309 BGB", findings: [] }],
} as ReviewSummary;

describe("SummaryBlock", () => {
  it("renders deterministic summary figures from the fixture", () => {
    render(<SummaryBlock reviewId="review-1" summary={summary} />);

    const block = screen.getByTestId("review-summary");
    expect(block).toHaveTextContent("2");
    expect(block).toHaveTextContent("1");
    expect(block).toHaveTextContent("Zwei Klauseln sollten überarbeitet werden.");
    expect(block).toHaveTextContent("6.1");
    expect(block).toHaveTextContent("§ 309 BGB (0)");
  });

  it("shows 409 blockers and links each one back to its finding", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/reviews/review-1/export`, () =>
        HttpResponse.json(
          {
            code: "export_blocked",
            message: "Offene Feststellungen",
            reason: "undecided",
            blocking: [{ position: 3, number: "6.1", heading: "Haftung", verdict: "uncertain" }],
          },
          { status: 409 },
        ),
      ),
    );
    render(<SummaryBlock reviewId="review-1" summary={summary} />);

    fireEvent.click(screen.getAllByRole("button", { name: "Exportieren" })[0]!);
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Exportieren" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "finale Export ist noch blockiert",
    );
    expect(within(dialog).getByRole("button", { name: "6.1 · Haftung" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: /als Entwurf exportieren/ }));
    expect(within(dialog).getByRole("checkbox")).toBeChecked();
  });
});

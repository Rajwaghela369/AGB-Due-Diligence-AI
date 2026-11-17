import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useReviewer } from "../decisions/reviewer";

import type { ReviewSummary } from "../../api/types.gen";
import "../../i18n";
import { server } from "../../test/server";
import { ORIGIN, installOriginFetch } from "../home/test-utils";
import { SummaryBlock } from "./summary-block";

installOriginFetch();
beforeEach(() => useReviewer.setState({ name: "Anna Müller", declined: false }));

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

  it("follows later changes to summary.draft and groups the format radios", () => {
    const { rerender } = render(<SummaryBlock reviewId="review-1" summary={summary} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Exportieren" })[0]!);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("checkbox")).toBeChecked();
    const radios = within(dialog).getAllByRole("radio");
    expect(radios).toHaveLength(3);
    expect(new Set(radios.map((radio) => radio.getAttribute("name"))).size).toBe(1);
    expect(radios[0]!.getAttribute("name")).toBeTruthy();

    rerender(<SummaryBlock reviewId="review-1" summary={{ ...summary, draft: false }} />);
    expect(within(screen.getByRole("dialog")).getByRole("checkbox")).not.toBeChecked();
  });

  it("explains a not_finished block instead of the generic message", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/reviews/review-1/export`, () =>
        HttpResponse.json(
          { code: "export_blocked", message: "x", reason: "not_finished", blocking: [] },
          { status: 409 },
        ),
      ),
    );
    render(<SummaryBlock reviewId="review-1" summary={summary} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Exportieren" })[0]!);
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Exportieren" }));
    await waitFor(() =>
      expect(within(dialog).getByRole("alert")).toHaveTextContent("Die Prüfung läuft noch."),
    );
  });

  it("sends the reviewer name with the export", async () => {
    let header: string | null = null;
    server.use(
      http.get(`${ORIGIN}/api/v1/reviews/review-1/export`, ({ request }) => {
        header = request.headers.get("X-Reviewer");
        return new HttpResponse("x", {
          headers: { "Content-Disposition": 'attachment; filename="a.docx"' },
        });
      }),
    );
    URL.createObjectURL = () => "blob:x";
    URL.revokeObjectURL = () => undefined;
    render(<SummaryBlock reviewId="review-1" summary={summary} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Exportieren" })[0]!);
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Exportieren" }),
    );
    await waitFor(() => expect(header).toBe(encodeURIComponent("Anna Müller")));
  });
});

describe("SummaryBlock collapsing", () => {
  const withFindings = {
    ...summary,
    counts: { ...summary.counts, findings: 7 },
  } as ReviewSummary;

  beforeEach(() => localStorage.clear());

  it("starts collapsed once there are findings, keeps export reachable, and remembers the choice", () => {
    const { unmount } = render(<SummaryBlock reviewId="review-1" summary={withFindings} />);
    const block = screen.getByTestId("review-summary");
    expect(block).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByText("Wichtigste Risiken")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Exportieren" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Zusammenfassung ausklappen" }));
    expect(screen.getByTestId("review-summary")).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByText("Wichtigste Risiken")).toBeInTheDocument();
    expect(localStorage.getItem("agb.summary.collapsed")).toBe("0");
    unmount();

    render(<SummaryBlock reviewId="review-1" summary={withFindings} />);
    expect(screen.getByTestId("review-summary")).toHaveAttribute("data-collapsed", "false");
  });

  it("stays expanded without findings and survives blocked storage", () => {
    render(<SummaryBlock reviewId="review-1" summary={summary} />);
    expect(screen.getByTestId("review-summary")).toHaveAttribute("data-collapsed", "false");
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    render(<SummaryBlock reviewId="review-1" summary={withFindings} />);
    expect(screen.getAllByTestId("review-summary")[1]).toHaveAttribute("data-collapsed", "true");
    spy.mockRestore();
  });
});

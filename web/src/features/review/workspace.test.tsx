import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Finding, FindingsOut, Review } from "../../api/types.gen";
import "../../i18n";
import { server } from "../../test/server";
import { useReviewUi } from "./store";
import { ReviewWorkspace } from "./workspace";

// The viewer and table have their own tests; here only the layout around them matters.
vi.mock("../viewer/document-viewer", () => ({
  DocumentViewer: () => <div data-testid="document-viewer" />,
}));
vi.mock("../table/findings-table", () => ({
  FindingsTable: () => <div data-testid="findings-table" />,
}));

const REVIEW = {
  review_id: "r1",
  document_id: "d1",
  agent_version: "v1",
  scope: ["liability"],
  status: "finished",
  counts: { total: 3, done: 3, failed: 0, potentially_void: 1, uncertain: 1, valid: 1 },
} as Review;

function finding(position: number, verdict: string): Finding {
  return {
    position,
    status: "done",
    clause: {
      position,
      number: `${position + 1}`,
      heading: `Klausel ${position}`,
      text: `Text ${position}`,
      content_hash: `h${position}`,
    },
    verdict: {
      agent_version: "v1",
      content_hash: `h${position}`,
      verdict,
      confidence: 0.9,
      legal_basis: [],
      reason_de: `Grund ${position}`,
    },
  } as Finding;
}

beforeEach(() => {
  useReviewUi.getState().reset();
  localStorage.clear();
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
    real(
      typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
      init,
    ),
  );
  server.use(
    http.get("http://localhost/api/v1/reviews/r1/findings", () =>
      HttpResponse.json({
        review: REVIEW,
        findings: [finding(0, "potentially_void"), finding(1, "uncertain"), finding(2, "valid")],
      } satisfies FindingsOut),
    ),
  );
});
afterEach(cleanup);

async function renderWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/reviews/r1"]}>
        <Routes>
          <Route path="reviews/:id" element={<ReviewWorkspace />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByTestId("finding-card-0");
}

describe("review workspace layout", () => {
  it("shows the risk summary as icon, label and count chips (bug 10)", async () => {
    await renderWorkspace();
    const chips = within(screen.getByTestId("risk-summary")).getAllByRole("listitem");
    expect(chips).toHaveLength(4);
    for (const chip of chips) expect(chip.querySelector("svg")).not.toBeNull();
    expect(chips[0]).toHaveTextContent("1 potenziell unwirksam");
    expect(chips[0]?.style.color).toBe("var(--risk)");
    expect(chips[1]).toHaveTextContent("1 zu prüfen");
    expect(chips[2]).toHaveTextContent("1 wirksam");
  });

  it("marks the selected view tab (bug 7)", async () => {
    await renderWorkspace();
    const views = screen.getByRole("tablist", { name: "Ansichten" });
    const document_ = within(views).getByRole("tab", { name: "Dokument" });
    const table = within(views).getByRole("tab", { name: "Tabelle" });
    expect(document_).toHaveAttribute("aria-selected", "true");
    expect(table).toHaveAttribute("aria-selected", "false");
    // The selected state is styled, not just announced.
    expect(document_.className).toContain("aria-selected:bg-foreground");
    fireEvent.click(table);
    expect(table).toHaveAttribute("aria-selected", "true");
    expect(document_).toHaveAttribute("aria-selected", "false");
    expect(screen.getByTestId("findings-table")).toBeInTheDocument();
  });

  it("never scrolls the page itself on selection (bug 2; layout is covered in e2e)", async () => {
    const pageScroll = vi.fn();
    window.scrollTo = pageScroll as typeof window.scrollTo;
    await renderWorkspace();
    fireEvent.click(within(screen.getByTestId("finding-card-1")).getAllByRole("button")[0]!);
    expect(window.scrollY).toBe(0);
    expect(pageScroll).not.toHaveBeenCalled();
  });

  it("moves focus into the detail and back to the card (focus management)", async () => {
    await renderWorkspace();
    const button = within(screen.getByTestId("finding-card-1")).getAllByRole("button")[0]!;
    button.focus();
    fireEvent.click(button);
    const drawer = await screen.findByTestId("finding-drawer");
    await waitFor(() =>
      expect(within(drawer).getByRole("button", { name: "Fundstellen" })).toHaveFocus(),
    );
    fireEvent.click(within(drawer).getByRole("button", { name: "Fundstellen" }));
    await waitFor(() => expect(button).toHaveFocus());
  });

  it("keeps the queue alongside the inspector while a finding is open", async () => {
    await renderWorkspace();
    fireEvent.click(within(screen.getByTestId("finding-card-0")).getAllByRole("button")[0]!);
    const drawer = await screen.findByTestId("finding-drawer");
    const column = drawer.parentElement as HTMLElement;
    expect(column).not.toContainElement(screen.getByTestId("findings-panel"));
    expect(column.closest(".workspace-grid")).toContainElement(
      screen.getByTestId("findings-panel"),
    );
    expect(column.parentElement).toContainElement(
      screen.getByRole("heading", { name: "Zusammenfassung" }),
    );
    // Not a floating dialog, and the document stays beside it.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTestId("document-viewer")).toBeInTheDocument();
    expect(screen.getByTestId("findings-panel")).not.toHaveClass("hidden");
    fireEvent.click(within(drawer).getByRole("button", { name: "Fundstellen" }));
    await waitFor(() => expect(screen.queryByTestId("finding-drawer")).toBeNull());
    expect(screen.getByTestId("findings-panel")).not.toHaveClass("hidden");
  });

  it("switches between document and findings on narrow screens (bug 15)", async () => {
    await renderWorkspace();
    const panes = screen.getByRole("tablist", { name: "Bereiche" });
    const documentTab = within(panes).getByRole("tab", { name: "Dokument" });
    const findingsTab = within(panes).getByRole("tab", { name: "Fundstellen" });
    expect(documentTab).toHaveAttribute("aria-selected", "true");
    fireEvent.click(findingsTab);
    expect(findingsTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("findings-panel").parentElement).toHaveClass("flex");
    // Picking a finding jumps to the document so the clause is in view.
    fireEvent.click(within(screen.getByTestId("finding-card-2")).getAllByRole("button")[0]!);
    expect(documentTab).toHaveAttribute("aria-selected", "true");
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../api/client";
import type { ReviewOut } from "../../api/types.gen";
import "../../i18n";
import { useReviewUi } from "../review/store";
import { DocumentViewer } from "./document-viewer";
import { documentOut, finding, pdfFindings, review, reviewData } from "./fixtures";
import type { PdfDocumentLike, PdfPageLike } from "./pdf";

const pdf = vi.hoisted(() => ({ openPdf: vi.fn() }));
vi.mock("./pdf", () => pdf);

const A4 = [0, 0, 595.28, 841.89] as const;

function fakePdf(pageCount = 2) {
  const render = vi.fn<PdfPageLike["render"]>(() => ({
    promise: Promise.resolve(),
    cancel: vi.fn(),
  }));
  const textItems = vi.fn(async () => [
    {
      str: "Unsere Haftung ist ausgeschlossen",
      transform: [12, 0, 0, 12, 72, 700],
      width: 200,
      height: 12,
    },
  ]);
  const pages: PdfPageLike[] = Array.from({ length: pageCount }, () => ({
    view: A4,
    render,
    textItems,
  }));
  const doc: PdfDocumentLike = {
    numPages: pageCount,
    getPage: vi.fn(async (n: number) => pages[n - 1]!),
    destroy: vi.fn(),
  };
  pdf.openPdf.mockResolvedValue(doc);
  return { doc, render, textItems };
}

function renderViewer(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const scrollIntoView = vi.fn();

beforeEach(() => {
  useReviewUi.getState().reset();
  scrollIntoView.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  vi.spyOn(api.documents, "file").mockResolvedValue(new Blob(["%PDF-1.7"]));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function pdfViewer(findings = pdfFindings(), base = review()) {
  const fake = fakePdf();
  const view = renderViewer(
    <DocumentViewer
      documentId="d1"
      review={reviewData(findings, base, { document: documentOut("pdf") })}
    />,
  );
  await screen.findByRole("region", { name: "Seite 2 von 2" });
  return { ...fake, ...view };
}

function lineBoxes(position: number): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>(`[data-clause-line="${position}"]`));
}

describe("PDF rendering", () => {
  it("renders the first page before any verdict, at its reserved size", async () => {
    const queued = pdfFindings().map((f) => ({ ...f, status: "queued" as const, verdict: null }));
    const { render: renderPage } = await pdfViewer(queued, review("running", { total: 6 }));

    const page1 = screen.getByRole("region", { name: "Seite 1 von 2" });
    // 595.28 × 841.89 pt at 96/72 px per pt.
    expect(parseFloat(page1.style.width)).toBeCloseTo(595.28 * (4 / 3), 2);
    expect(parseFloat(page1.style.height)).toBeCloseTo(841.89 * (4 / 3), 2);
    await waitFor(() => expect(renderPage).toHaveBeenCalled());
    expect(renderPage.mock.calls[0]![0]).toMatchObject({ scale: 4 / 3, ratio: 1 });
    expect(api.documents.file).toHaveBeenCalledWith("d1");
  });

  it("positions each clause line from its rect and the zoom", async () => {
    await pdfViewer();
    const [first, second] = lineBoxes(0);
    // rect(1, 100): x0 72, top 100, x1 504, bottom 112 → ×4/3.
    expect(first!.style).toMatchObject({
      left: "96px",
      top: "133px",
      width: "576px",
      height: "16px",
    });
    expect(second!.style.top).toBe("149px");

    fireEvent.click(screen.getByRole("button", { name: "Vergrößern" }));
    fireEvent.click(screen.getByRole("button", { name: "Vergrößern" }));
    expect(screen.getByRole("button", { name: /Zoom 125 %/ })).toBeInTheDocument();
    // 1.25 × 4/3 = 5/3 px per pt.
    expect(lineBoxes(0)[0]!.style).toMatchObject({ left: "120px", width: "720px" });
    const zoomed = screen.getByRole("region", { name: "Seite 1 von 2" });
    expect(parseFloat(zoomed.style.width)).toBeCloseTo(595.28 * (5 / 3), 2);

    fireEvent.click(screen.getByRole("button", { name: /Zoom 125 %/ }));
    expect(lineBoxes(0)[0]!.style.left).toBe("96px");
  });

  it("draws every state with its tone, and nothing for out of scope", async () => {
    await pdfViewer();
    const tone = (position: number) =>
      document.querySelector(`[data-clause="${position}"]`)?.getAttribute("data-tone");
    expect([0, 1, 2, 3, 4, 5].map(tone)).toEqual([
      "void",
      "review",
      "valid",
      "queued",
      "checking",
      "failed",
    ]);
    expect(document.querySelector('[data-clause-bar="3"]')?.getAttribute("data-bar")).toBe(
      "dashed",
    );
    expect(document.querySelector('[data-clause-bar="2"]')?.getAttribute("data-bar")).toBe("faint");
    expect(lineBoxes(2)[0]!.style.background).toBe("");
    expect(lineBoxes(0)[0]!.style.background).toContain("var(--risk)");
    expect(document.querySelector('[data-clause="4"]')?.className).toContain(
      "motion-reduce:animate-none",
    );
    // Out of scope: an invisible scroll anchor only.
    expect(document.querySelector('[data-clause="6"]')).toBeNull();
    expect(document.querySelector('[data-clause-anchor="6"]')).not.toBeNull();
    // Icons with labels, never colour alone.
    expect(screen.getByRole("button", { name: "Klausel 1.1: Potenziell unwirksam" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Klausel 3.1: Wirksam" })).toBeVisible();
  });

  it("puts a clause's marker on its first page only", async () => {
    await pdfViewer();
    const page2 = screen.getByRole("region", { name: "Seite 2 von 2" });
    expect(within(page2).queryByRole("button", { name: /Klausel 2\.1/ })).toBeNull();
    expect(page2.querySelector('[data-clause-line="1"]')).not.toBeNull();
  });

  it("retries a failed clause", async () => {
    const retry = vi.spyOn(api.reviews, "retryClause").mockResolvedValue({} as ReviewOut);
    await pdfViewer();
    expect(screen.getByText("Konnte nicht geprüft werden")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Klausel 6.1 erneut prüfen" }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith("r1", 5));
  });
});

describe("shared selection", () => {
  it("scrolls the selected clause into view, centred", async () => {
    await pdfViewer();
    act(() => useReviewUi.getState().select(2));
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    const target = scrollIntoView.mock.contexts.at(-1) as HTMLElement;
    expect(target.getAttribute("data-clause-anchor")).toBe("2");
    expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "center", behavior: "smooth" });
  });

  it("scrolls without animation when reduced motion is preferred", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("reduced-motion"),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    try {
      await pdfViewer();
      act(() => useReviewUi.getState().select(1));
      await waitFor(() =>
        expect(scrollIntoView).toHaveBeenLastCalledWith({ block: "center", behavior: "auto" }),
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("highlights a clause hovered in another panel", async () => {
    await pdfViewer();
    const clause = () => document.querySelector('[data-clause="1"]')!;
    expect(clause().hasAttribute("data-emphasised")).toBe(false);
    act(() => useReviewUi.getState().hover(1));
    expect(clause().hasAttribute("data-emphasised")).toBe(true);
    expect(lineBoxes(1)[0]!.style.background).toContain("26%");
    act(() => useReviewUi.getState().hover(null));
    expect(clause().hasAttribute("data-emphasised")).toBe(false);
  });

  it("hovering and clicking a clause write to the shared state", async () => {
    await pdfViewer();
    fireEvent.mouseEnter(lineBoxes(0)[1]!);
    expect(useReviewUi.getState().hovered).toBe(0);
    fireEvent.mouseLeave(lineBoxes(0)[1]!);
    expect(useReviewUi.getState().hovered).toBeNull();
    fireEvent.click(lineBoxes(0)[0]!);
    expect(useReviewUi.getState().selected).toBe(0);
  });
});

describe("toolbar", () => {
  it("steps through the navigable findings with ◀ and ▶", async () => {
    await pdfViewer();
    act(() => useReviewUi.getState().setNavigable([0, 1, 5]));
    const position = screen.getByTestId("finding-position");
    expect(position).toHaveTextContent("– / 3");

    fireEvent.click(screen.getByRole("button", { name: "Nächste Fundstelle" }));
    expect(useReviewUi.getState().selected).toBe(0);
    expect(position).toHaveTextContent("1 / 3");
    fireEvent.click(screen.getByRole("button", { name: "Nächste Fundstelle" }));
    expect(position).toHaveTextContent("2 / 3");
    fireEvent.click(screen.getByRole("button", { name: "Vorherige Fundstelle" }));
    fireEvent.click(screen.getByRole("button", { name: "Vorherige Fundstelle" }));
    expect(useReviewUi.getState().selected).toBe(5);
    expect(position).toHaveTextContent("3 / 3");
  });

  it("disables navigation when there is nothing to step through", async () => {
    await pdfViewer();
    expect(screen.getByRole("button", { name: "Nächste Fundstelle" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Verkleinern" })).toBeEnabled();
  });

  it("limits zoom to 75%–200%", async () => {
    await pdfViewer();
    const out = screen.getByRole("button", { name: "Verkleinern" });
    fireEvent.click(out);
    fireEvent.click(out);
    expect(out).toBeDisabled();
    expect(screen.getByRole("button", { name: /Zoom 75 %/ })).toBeInTheDocument();
    const zoomIn = screen.getByRole("button", { name: "Vergrößern" });
    for (let i = 0; i < 10; i++) fireEvent.click(zoomIn);
    expect(zoomIn).toBeDisabled();
    expect(screen.getByRole("button", { name: /Zoom 200 %/ })).toBeInTheDocument();
  });

  it("searches the PDF text and highlights hits", async () => {
    const { textItems } = await pdfViewer();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "haftung" } });
    await waitFor(() => expect(document.querySelectorAll("[data-hit-index]")).toHaveLength(2));
    expect(textItems).toHaveBeenCalled();
    // The count reaches the toolbar one render after the hits are drawn.
    expect(await screen.findByText("1 / 2")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Enter" });
    expect(await screen.findByText("2 / 2")).toBeInTheDocument();
    expect(document.querySelector('[data-hit-index="1"]')?.hasAttribute("data-active")).toBe(true);
  });
});

describe("step bar", () => {
  it("shows progress while the review runs", async () => {
    await pdfViewer(pdfFindings(), review("running", { total: 31, done: 6, failed: 1 }));
    const steps = screen.getByRole("list", { name: "Fortschritt der Prüfung" });
    expect(steps).toHaveTextContent("Upload");
    expect(steps).toHaveTextContent("Lesen");
    expect(steps).toHaveTextContent("Aufteilen");
    expect(steps).toHaveTextContent("Prüfung 7/31");
  });

  it("is hidden once the review is finished", async () => {
    await pdfViewer();
    expect(screen.queryByRole("list", { name: "Fortschritt der Prüfung" })).toBeNull();
  });
});

describe("waiting reason", () => {
  it("tells the lawyer why the check is paused", async () => {
    const fake = fakePdf();
    expect(fake.doc.numPages).toBe(2);
    const progress = { waiting_reason: "daily_budget" as const, budget_left: 0 };
    renderViewer(
      <DocumentViewer
        documentId="d1"
        review={reviewData(pdfFindings(), review("running", { total: 6 }), {
          document: documentOut("pdf"),
          progress,
        })}
      />,
    );
    expect(await screen.findByText("Tagesbudget erreicht")).toBeVisible();
  });
});

describe("pasted text", () => {
  const textFindings = () => [
    finding(
      0,
      "done",
      {
        text: "6.1 Unsere Haftung ist ausgeschlossen.",
        start: 12,
        end: 50,
        heading: "§ 6 Haftung",
      },
      "potentially_void",
    ),
    finding(1, "checking", {
      text: "6.2 Die Gewährleistung richtet sich nach dem Gesetz.",
      start: 51,
      end: 103,
      heading: "§ 6 Haftung",
    }),
    finding(2, "out_of_scope", {
      text: "7.1 Gerichtsstand ist Berlin.",
      start: 104,
      end: 133,
      heading: "§ 7 Schluss",
    }),
  ];

  const SOURCE = [
    "§ 6 Haftung\n",
    "6.1 Unsere Haftung ist ausgeschlossen.\n",
    "6.2 Die Gewährleistung richtet sich nach dem Gesetz.\n",
    "7.1 Gerichtsstand ist Berlin.",
  ].join("");
  const textData = () => reviewData(textFindings(), review(), { document: documentOut("text") });

  beforeEach(() => {
    vi.spyOn(api.documents, "text").mockResolvedValue(SOURCE);
  });

  it("renders the text in serif with the same highlight model, no PDF", async () => {
    renderViewer(<DocumentViewer documentId="d1" review={textData()} />);
    const clause = await screen.findByText("6.1 Unsere Haftung ist ausgeschlossen.");
    expect(clause.closest("article")?.className).toContain("font-contract");
    expect(clause.closest("[data-clause]")?.getAttribute("data-tone")).toBe("void");
    expect(clause.closest("p")?.style.background).toContain("var(--risk)");
    expect(document.querySelector('[data-clause="1"]')?.getAttribute("data-tone")).toBe("checking");
    expect(document.querySelector('[data-clause="2"]')?.getAttribute("data-tone")).toBe("out");
    // Text between clauses comes from the stored source, not from the clause headings.
    expect(screen.getByText("§ 6 Haftung")).toBeInTheDocument();
    expect(api.documents.text).toHaveBeenCalledWith("d1");
    expect(pdf.openPdf).not.toHaveBeenCalled();
    expect(api.documents.file).not.toHaveBeenCalled();
  });

  it("falls back to the clause headings when the source cannot be fetched", async () => {
    vi.spyOn(api.documents, "text").mockRejectedValue(new Error("offline"));
    renderViewer(<DocumentViewer documentId="d1" review={textData()} />);
    await screen.findByText("6.1 Unsere Haftung ist ausgeschlossen.");
    expect(screen.getByRole("heading", { name: "§ 6 Haftung" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "§ 7 Schluss" })).toBeInTheDocument();
  });

  it("scrolls to, hovers and selects clauses by offset blocks", async () => {
    renderViewer(<DocumentViewer documentId="d1" review={textData()} />);
    const clause = await screen.findByText("6.2 Die Gewährleistung richtet sich nach dem Gesetz.");
    act(() => useReviewUi.getState().select(1));
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    expect((scrollIntoView.mock.contexts.at(-1) as HTMLElement).getAttribute("data-clause")).toBe(
      "1",
    );
    act(() => useReviewUi.getState().hover(0));
    expect(document.querySelector('[data-clause="0"]')?.hasAttribute("data-emphasised")).toBe(true);
    fireEvent.mouseEnter(clause);
    expect(useReviewUi.getState().hovered).toBe(1);
  });

  it("marks search hits in the text", async () => {
    renderViewer(<DocumentViewer documentId="d1" review={textData()} />);
    await screen.findByText("6.1 Unsere Haftung ist ausgeschlossen.");
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "ist" } });
    const marks = document.querySelectorAll("mark[data-hit-index]");
    // "Haftung ist", "Gewährleistung" and "Gerichtsstand ist".
    expect(marks).toHaveLength(3);
    expect(marks[0]).toHaveTextContent("ist");
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
  });
});

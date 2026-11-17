import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { ClauseVerdict } from "../../api/types.gen";
import { useReviewUi } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import { FindingsTable } from "./findings-table";

const TOPICS = ["Haftung", "Gewährleistung", "Laufzeit"] as const;
const VERDICTS = ["potentially_void", "uncertain", "valid", null] as const;

function makeFindings(count: number): ReviewFinding[] {
  return Array.from({ length: count }, (_, position) => {
    const verdict = VERDICTS[position % 4] ?? null;
    return {
      position,
      clause: {
        position,
        content_hash: `h${position}`,
        text: `Klausel ${position}${position % 5 === 0 ? " mit Schadensersatz" : ""}`,
        topic: TOPICS[position % 3],
      },
      status: verdict ? "done" : "queued",
      verdict: verdict
        ? ({
            verdict,
            confidence: position % 2 ? 0.9 : 0.5,
            legal_basis: [`§ ${300 + (position % 7)} BGB`],
          } as ClauseVerdict)
        : null,
      error: null,
      decision: null,
      feedback: null,
    } as ReviewFinding;
  });
}

function reviewData(findings: ReviewFinding[]): ReviewData {
  return {
    review: null,
    findings,
    byPosition: new Map(findings.map((f) => [f.position, f])),
    isLoading: false,
    error: null,
    isFinal: true,
    progress: null,
    document: null,
    refresh: () => Promise.resolve(),
  };
}

const FINDINGS = makeFindings(300);

afterEach(cleanup);

beforeEach(() => {
  useReviewUi.getState().reset();
  // jsdom has no layout; give the scroll container a 600px viewport for the virtualizer.
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(600);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(900);
});

function dataRows() {
  return screen.getAllByRole("row").filter((row) => row.tagName === "BUTTON");
}

describe("FindingsTable", () => {
  it("renders only the visible rows of a 300-clause review", () => {
    render(<FindingsTable review={reviewData(FINDINGS)} />);

    expect(screen.getByText("300 Klauseln")).toBeInTheDocument();
    const rendered = dataRows().length;
    expect(rendered).toBeGreaterThan(0);
    expect(rendered).toBeLessThan(40);
  });

  const cases: Array<[string, string, string]> = [];
  for (const verdict of ["all", "potentially_void", "uncertain", "valid"]) {
    for (const topic of ["all", ...TOPICS]) {
      for (const search of ["", "schadensersatz"]) cases.push([verdict, topic, search]);
    }
  }

  it.each(cases)("filters by verdict=%s topic=%s search=%j", (verdict, topic, search) => {
    const expected = FINDINGS.filter(
      (f) =>
        (verdict === "all" || f.verdict?.verdict === verdict) &&
        (topic === "all" || f.clause.topic === topic) &&
        f.clause.text.toLowerCase().includes(search),
    ).length;
    render(<FindingsTable review={reviewData(FINDINGS)} />);

    fireEvent.change(screen.getByLabelText("Bewertung"), { target: { value: verdict } });
    fireEvent.change(screen.getByLabelText("Thema"), { target: { value: topic } });
    fireEvent.change(screen.getByLabelText("Klauseln durchsuchen"), { target: { value: search } });

    expect(screen.getByText(`${expected} Klauseln`)).toBeInTheDocument();
  });

  it("sorts a column and flags the direction", () => {
    render(<FindingsTable review={reviewData(FINDINGS)} />);
    const header = screen.getByRole("columnheader", { name: /Nr\./ });

    fireEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(dataRows()[0]).toHaveTextContent("Klausel 299");
  });

  it("sorts verdicts by severity", () => {
    render(<FindingsTable review={reviewData(FINDINGS)} />);
    const header = screen.getByRole("columnheader", { name: /Bewertung/ });
    fireEvent.click(within(header).getByRole("button"));
    fireEvent.click(within(header).getByRole("button"));

    expect(dataRows()[0]).toHaveTextContent("Potenziell unwirksam");
  });

  it("selects a row, opens the document view and syncs hover", () => {
    useReviewUi.getState().setView("table");
    render(<FindingsTable review={reviewData(FINDINGS)} />);
    const row = dataRows()[2]!;

    fireEvent.mouseEnter(row);
    expect(useReviewUi.getState().hovered).toBe(2);
    fireEvent.mouseLeave(row);
    expect(useReviewUi.getState().hovered).toBeNull();

    fireEvent.click(row);
    expect(useReviewUi.getState().selected).toBe(2);
    expect(useReviewUi.getState().view).toBe("document");
  });

  it("highlights the selected row and a row hovered elsewhere", () => {
    useReviewUi.getState().select(1);
    useReviewUi.getState().hover(3);
    render(<FindingsTable review={reviewData(FINDINGS)} />);

    const rows = dataRows();
    expect(rows[1]).toHaveAttribute("aria-selected", "true");
    expect(rows[0]).toHaveAttribute("aria-selected", "false");
    expect(rows[3]).toHaveClass("bg-muted");
  });

  it("shows icon and label for every verdict, never colour alone", () => {
    render(<FindingsTable review={reviewData(FINDINGS.slice(0, 4))} />);
    const [first, second, third, fourth] = dataRows();

    expect(first).toHaveTextContent("Potenziell unwirksam");
    expect(second).toHaveTextContent("Zu prüfen");
    expect(third).toHaveTextContent("Wirksam");
    expect(fourth).toHaveTextContent("Wartet");
    expect(first!.querySelector("svg")).not.toBeNull();
  });

  it("shows an em dash when a clause has no topic", () => {
    const [finding] = makeFindings(1);
    finding!.clause.topic = null;
    render(<FindingsTable review={reviewData([finding!])} />);

    expect(within(dataRows()[0]!).getByRole("gridcell", { name: "—" })).toBeInTheDocument();
  });

  it("keeps every fixed-size column reachable in one scrolling region", () => {
    render(<FindingsTable review={reviewData(FINDINGS.slice(0, 1))} />);

    const scroller = screen.getByTestId("findings-scroll");
    Object.defineProperties(scroller, {
      clientWidth: { configurable: true, value: 900 },
      scrollWidth: { configurable: true, value: 1120 },
    });
    expect(scroller).toHaveClass("overflow-auto");
    expect(screen.getByRole("columnheader", { name: /Sicherheit/ }).parentElement).toHaveClass(
      "sticky",
    );
    expect(screen.getByRole("gridcell", { name: "Haftung" })).toBeInTheDocument();
    expect(scroller.scrollWidth).toBeGreaterThan(scroller.clientWidth);
  });

  it("scrolls a selected row below the sticky header", () => {
    const scrollTo = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: scrollTo,
    });
    useReviewUi.getState().select(20);
    render(<FindingsTable review={reviewData(FINDINGS)} />);

    const [{ top }] = scrollTo.mock.calls.at(-1)!;
    const rowTop = 20 * 58;
    const headerHeight = 48;

    expect(top).toBe(618);
    expect(rowTop - top).toBeGreaterThanOrEqual(headerHeight);
    expect(screen.getByTestId("findings-table-header")).toHaveClass("sticky");
  });
});

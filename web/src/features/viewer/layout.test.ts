import { describe, expect, it } from "vitest";

import { finding, pdfFindings, rect, review } from "./fixtures";
import { clausesByPage, hitOffsets, inferSourceType, reviewProgress } from "./layout";

describe("layout helpers", () => {
  it("places each clause on its pages, marking the first", () => {
    const byPage = clausesByPage(pdfFindings());
    expect(byPage.get(1)?.map((c) => [c.finding.position, c.first])).toEqual([
      [0, true],
      [1, true],
    ]);
    const second = byPage.get(2)!;
    expect(second[0]).toMatchObject({ first: false, rects: [rect(2, 72)] });
    expect(second.map((c) => c.finding.position)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it("infers the source type from the clauses", () => {
    expect(inferSourceType(pdfFindings())).toBe("pdf");
    expect(inferSourceType([finding(0, "queued", { start: 0, end: 5 })])).toBe("text");
    expect(inferSourceType([finding(0, "queued")])).toBeNull();
  });

  it("counts checked clauses for the step bar", () => {
    expect(reviewProgress(review("running", { total: 31, done: 6, failed: 1 }))).toEqual({
      done: 7,
      total: 31,
    });
    expect(reviewProgress(review("running"))).toEqual({ done: 0, total: 0 });
  });

  it("counts awaiting decisions as judged and excludes out-of-scope clauses", () => {
    expect(
      reviewProgress(review("running", { total: 35, done: 34, awaiting_decision: 1 })),
    ).toEqual({ done: 35, total: 35 });
  });

  it("offsets search hits across pages", () => {
    expect(hitOffsets([[1, 2], [], [3]])).toEqual([0, 2, 2]);
  });
});

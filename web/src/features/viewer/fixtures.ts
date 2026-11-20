// Test fixtures for the viewer: findings in every state, on two PDF pages or as pasted text.
import type { ClauseSpan, ClauseVerdict, DocumentOut, Review } from "../../api/types.gen";
import type { ReviewData, ReviewFinding } from "../review/use-review";

export function verdict(kind: ClauseVerdict["verdict"]): ClauseVerdict {
  return {
    verdict: kind,
    confidence: 0.9,
    legal_basis: ["§ 309 Nr. 7 BGB"],
    reason_de: "Begründung",
  } as unknown as ClauseVerdict;
}

export function finding(
  position: number,
  status: ReviewFinding["status"],
  clause: Partial<ClauseSpan> = {},
  kind: ClauseVerdict["verdict"] | null = null,
): ReviewFinding {
  return {
    position,
    status,
    verdict: kind === null ? null : verdict(kind),
    error: status === "failed" ? "boom" : null,
    decision: null,
    findingStatus: "open",
    feedback: null,
    clause: {
      position,
      content_hash: `h${position}`,
      text: `Klausel ${position}`,
      number: `${position + 1}.1`,
      ...clause,
    },
  };
}

export function rect(page: number, top: number, x0 = 72, x1 = 504) {
  return { page, x0, x1, top, bottom: top + 12 };
}

export function review(
  status: Review["status"] = "finished",
  counts: Review["counts"] = {},
): Review {
  return {
    review_id: "r1",
    document_id: "d1",
    agent_version: "v1",
    scope: ["liability"],
    status,
    counts,
  };
}

export function reviewData(
  findings: ReviewFinding[],
  base: Review = review(),
  extra: Partial<ReviewData> = {},
): ReviewData {
  return {
    review: base,
    findings,
    byPosition: new Map(findings.map((f) => [f.position, f])),
    isLoading: false,
    error: null,
    isFinal: ["finished", "failed"].includes(base.status),
    progress: null,
    document: null,
    refresh: () => Promise.resolve(),
    ...extra,
  };
}

/** Two pages; clause 1 runs from the foot of page 1 onto page 2. */
export function pdfFindings(): ReviewFinding[] {
  return [
    finding(0, "done", { rects: [rect(1, 100), rect(1, 112)], page: 1 }, "potentially_void"),
    finding(1, "done", { rects: [rect(1, 780), rect(2, 72)], page: 1 }, "uncertain"),
    finding(2, "done", { rects: [rect(2, 200)], page: 2 }, "valid"),
    finding(3, "queued", { rects: [rect(2, 300)], page: 2 }),
    finding(4, "checking", { rects: [rect(2, 400)], page: 2 }),
    finding(5, "failed", { rects: [rect(2, 500)], page: 2 }),
    finding(6, "out_of_scope", { rects: [rect(2, 600)], page: 2, in_scope: false }),
  ];
}

export function documentOut(sourceType: "pdf" | "text"): DocumentOut {
  return {
    document: {
      document_id: "d1",
      filename: "AGB.pdf",
      source_type: sourceType,
      parser_version: "p",
      splitter_version: "s",
      text_hash: "t",
    },
    clauses: [],
  };
}

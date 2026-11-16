// Pure helpers shared by the viewer's components: how findings map onto pages, how a clause
// is named and barred, and how far a review has got. Kept out of the .tsx files so they are
// unit-testable and Fast Refresh keeps working.
import type { CSSProperties } from "react";

import type { Review, Rect } from "../../api/types.gen";
import type { ReviewFinding } from "../review/use-review";
import { rectsByPage } from "./geometry";
import type { HighlightStyle } from "./highlight";

/** What every clause highlight needs from the viewer. */
export interface ClauseInteraction {
  hovered: number | null;
  selected: number | null;
  hover: (position: number | null) => void;
  select: (position: number | null) => void;
  retry: (position: number) => void;
  /** Positions whose retry request is in flight. */
  retrying: ReadonlySet<number>;
}

export interface PageClause {
  finding: ReviewFinding;
  rects: Rect[];
  /** This page holds the clause's first line (marker, retry and scroll anchor go here). */
  first: boolean;
}

export const pulseClass = "animate-pulse motion-reduce:animate-none";

export function clauseName(finding: ReviewFinding): string {
  return finding.clause.number?.trim() || `#${finding.position + 1}`;
}

/** CSS for the margin bar of a clause in its tone. */
export function barCss(style: HighlightStyle): CSSProperties {
  switch (style.bar) {
    case "solid":
      return { background: style.color };
    case "faint":
      return { background: style.color, opacity: 0.45 };
    case "dashed":
      return { borderLeft: `2px dashed ${style.color}` };
    case "none":
      return {};
  }
}

/**
 * Source type from the clauses themselves, so rendering can start before (or without) the
 * document request: PDF clauses carry rects, pasted-text clauses carry offsets.
 */
export function inferSourceType(findings: ReviewFinding[]): "pdf" | "text" | null {
  if (findings.some((f) => (f.clause.rects?.length ?? 0) > 0)) return "pdf";
  if (findings.some((f) => typeof f.clause.start === "number")) return "text";
  return null;
}

/** Each page's clause rects; the first page a clause appears on carries its marker. */
export function clausesByPage(findings: ReviewFinding[]): Map<number, PageClause[]> {
  const result = new Map<number, PageClause[]>();
  for (const finding of findings) {
    let first = true;
    const pages = [...rectsByPage(finding.clause.rects ?? [])].sort((a, b) => a[0] - b[0]);
    for (const [page, rects] of pages) {
      const list = result.get(page) ?? [];
      list.push({ finding, rects, first });
      result.set(page, list);
      first = false;
    }
  }
  return result;
}

/** The global index of each page's first search hit. */
export function hitOffsets(hitsByPage: ReadonlyArray<readonly unknown[]>): number[] {
  const offsets: number[] = [];
  let total = 0;
  for (const hits of hitsByPage) {
    offsets.push(total);
    total += hits.length;
  }
  return offsets;
}

/** Checked clauses (done or failed) out of all in-scope clauses. */
export function reviewProgress(review: Review): { done: number; total: number } {
  const counts = review.counts ?? {};
  return { done: (counts.done ?? 0) + (counts.failed ?? 0), total: counts.total ?? 0 };
}

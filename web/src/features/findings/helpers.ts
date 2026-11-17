// Small pure helpers shared by the findings components.
import { clauseTitle as formatClauseTitle } from "../review/clause-title";
import type { ReviewFinding } from "../review/use-review";

/** „3.5 · Lieferung": one formatter for cards, drawer and table; the clause data stays as stored. */
export function clauseTitle(finding: ReviewFinding): string {
  return formatClauseTitle(finding.clause);
}

/** Free text for the § list: one entry per comma, semicolon or line. */
export function parseBasis(text: string): string[] {
  return text
    .split(/[,;\n]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** The developer switch that reveals per-step votes: `localStorage["agb.dev"] === "1"`. */
export function isDevMode(): boolean {
  try {
    return localStorage.getItem("agb.dev") === "1";
  } catch {
    return false;
  }
}

export function langfuseTraceUrl(traceId: string | null | undefined): string | null {
  const base = import.meta.env.VITE_LANGFUSE_URL;
  if (!traceId || !base) return null;
  return `${base.replace(/\/$/, "")}/trace/${encodeURIComponent(traceId)}`;
}

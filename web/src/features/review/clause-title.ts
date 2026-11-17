/** Stable clause identity without repeating the section number carried by its heading. */
import type { ClauseSpan } from "../../api/types.gen";

const LEADING_MARKER = /^(?:§\s*\d+[a-z]?|\d+(?:\.\d+)*\.?|\(\d+\)|[a-z]\))\s*/i;

export function clauseTitle(clause: ClauseSpan): string {
  const heading = clause.heading?.replace(LEADING_MARKER, "").trim();
  const separator = heading?.startsWith("·") ? " " : " · ";
  const identity = [clause.number, heading].filter(Boolean).join(separator);
  return identity || `#${clause.position + 1}`;
}

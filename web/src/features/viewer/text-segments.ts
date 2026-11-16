// Pasted-text documents have no pages, only character offsets (`start`/`end`) into the
// normalised text. This turns clauses into an ordered list of blocks the text view renders
// with the same highlight model as the PDF.
//
// The API does not serve the pasted source text (`/documents/{id}/file` is PDF-only), so
// without it the clause texts are laid out in offset order and section headings are
// restored from `clause.heading`. When a source string is supplied, blocks are sliced from
// it exactly, including the text between clauses.
import type { ClauseSpan } from "../../api/types.gen";

export type TextBlock =
  | { kind: "clause"; position: number; text: string }
  | { kind: "heading"; text: string }
  | { kind: "gap"; text: string };

type OffsetClause = ClauseSpan & { start: number; end: number };

function hasOffsets(clause: ClauseSpan): clause is OffsetClause {
  return typeof clause.start === "number" && typeof clause.end === "number";
}

export function textBlocks(clauses: ClauseSpan[], source?: string): TextBlock[] {
  const ordered = [...clauses].sort((a, b) => {
    const left = hasOffsets(a) ? a.start : Number.MAX_SAFE_INTEGER;
    const right = hasOffsets(b) ? b.start : Number.MAX_SAFE_INTEGER;
    return left - right || a.position - b.position;
  });

  if (source !== undefined) return fromSource(ordered, source);

  const blocks: TextBlock[] = [];
  let heading: string | null = null;
  for (const clause of ordered) {
    const next = clause.heading?.trim() || null;
    if (next !== null && next !== heading && !startsWithHeading(clause.text, next)) {
      blocks.push({ kind: "heading", text: next });
    }
    heading = next;
    blocks.push({ kind: "clause", position: clause.position, text: clause.text });
  }
  return blocks;
}

function fromSource(ordered: ClauseSpan[], source: string): TextBlock[] {
  const blocks: TextBlock[] = [];
  let cursor = 0;
  for (const clause of ordered) {
    if (!hasOffsets(clause) || clause.start < cursor) {
      // Overlapping or offset-less clauses fall back to their own text.
      blocks.push({ kind: "clause", position: clause.position, text: clause.text });
      continue;
    }
    const gap = source.slice(cursor, clause.start);
    if (gap.trim()) blocks.push({ kind: "gap", text: gap.trim() });
    blocks.push({
      kind: "clause",
      position: clause.position,
      text: source.slice(clause.start, clause.end),
    });
    cursor = clause.end;
  }
  const tail = source.slice(cursor);
  if (tail.trim()) blocks.push({ kind: "gap", text: tail.trim() });
  return blocks;
}

function startsWithHeading(text: string, heading: string): boolean {
  const squash = (value: string) => value.replace(/\s+/g, " ").trim().toLowerCase();
  return squash(text).startsWith(squash(heading));
}

/** Case-insensitive, non-overlapping [start, end) ranges of `query` in `text`. */
export function findMatches(text: string, query: string): Array<[number, number]> {
  const needle = query.trim().toLowerCase();
  const haystack = text.toLowerCase();
  if (needle.length === 0 || haystack.length !== text.length) return [];
  const ranges: Array<[number, number]> = [];
  let from = haystack.indexOf(needle);
  while (from !== -1) {
    ranges.push([from, from + needle.length]);
    from = haystack.indexOf(needle, from + needle.length);
  }
  return ranges;
}

/** Splits text into plain and matched runs for rendering `<mark>`s. */
export function splitByMatches(
  text: string,
  ranges: Array<[number, number]>,
): Array<{ text: string; match: boolean }> {
  const runs: Array<{ text: string; match: boolean }> = [];
  let cursor = 0;
  for (const [start, end] of ranges) {
    if (start > cursor) runs.push({ text: text.slice(cursor, start), match: false });
    runs.push({ text: text.slice(start, end), match: true });
    cursor = end;
  }
  if (cursor < text.length) runs.push({ text: text.slice(cursor), match: false });
  return runs;
}

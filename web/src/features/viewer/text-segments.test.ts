import { describe, expect, it } from "vitest";

import type { ClauseSpan } from "../../api/types.gen";
import { findMatches, splitByMatches, textBlocks } from "./text-segments";

const source =
  "§ 6 Haftung\n6.1 Unsere Haftung ist ausgeschlossen.\n6.2 Die Gewährleistung richtet sich nach dem Gesetz.\nSchluss";

function clause(position: number, text: string, extra: Partial<ClauseSpan> = {}): ClauseSpan {
  const start = source.indexOf(text);
  return {
    position,
    text,
    content_hash: `h${position}`,
    start,
    end: start + text.length,
    ...extra,
  };
}

const clauses = [
  clause(1, "6.2 Die Gewährleistung richtet sich nach dem Gesetz.", { heading: "§ 6 Haftung" }),
  clause(0, "6.1 Unsere Haftung ist ausgeschlossen.", { heading: "§ 6 Haftung" }),
];

describe("text blocks from offsets", () => {
  it("orders clauses by offset and restores the section heading once", () => {
    expect(textBlocks(clauses)).toEqual([
      { kind: "heading", text: "§ 6 Haftung" },
      { kind: "clause", position: 0, text: "6.1 Unsere Haftung ist ausgeschlossen." },
      { kind: "clause", position: 1, text: "6.2 Die Gewährleistung richtet sich nach dem Gesetz." },
    ]);
  });

  it("does not repeat a heading the clause already starts with", () => {
    const own = [{ ...clauses[1]!, heading: "6.1 Unsere" }];
    expect(textBlocks(own).map((b) => b.kind)).toEqual(["clause"]);
  });

  it("slices exactly from the source, keeping the text between clauses", () => {
    expect(textBlocks(clauses, source)).toEqual([
      { kind: "gap", text: "§ 6 Haftung" },
      { kind: "clause", position: 0, text: "6.1 Unsere Haftung ist ausgeschlossen." },
      { kind: "clause", position: 1, text: "6.2 Die Gewährleistung richtet sich nach dem Gesetz." },
      { kind: "gap", text: "Schluss" },
    ]);
  });

  it("falls back to clause text when offsets are missing", () => {
    const loose: ClauseSpan = { position: 2, text: "Ohne Offsets", content_hash: "x" };
    expect(textBlocks([loose, ...clauses], source).at(-2)).toEqual({
      kind: "clause",
      position: 2,
      text: "Ohne Offsets",
    });
  });
});

describe("matches", () => {
  it("finds case-insensitive, non-overlapping matches", () => {
    expect(findMatches("Haftung haftung HAFTUNG", "haftung")).toEqual([
      [0, 7],
      [8, 15],
      [16, 23],
    ]);
    expect(findMatches("aaaa", "aa")).toEqual([
      [0, 2],
      [2, 4],
    ]);
    expect(findMatches("text", " ")).toEqual([]);
  });

  it("splits text into plain and matched runs", () => {
    expect(splitByMatches("ab Haftung cd", [[3, 10]])).toEqual([
      { text: "ab ", match: false },
      { text: "Haftung", match: true },
      { text: " cd", match: false },
    ]);
  });
});

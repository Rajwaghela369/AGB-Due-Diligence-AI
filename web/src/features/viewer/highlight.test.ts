import { describe, expect, it } from "vitest";

import { emphasisTint, highlightStyle } from "./highlight";
import { verdict } from "./fixtures";

const style = (
  status: Parameters<typeof highlightStyle>[0]["status"],
  kind?: "valid" | "potentially_void" | "uncertain",
) => highlightStyle({ status, verdict: kind ? verdict(kind) : null });

describe("tone → highlight style", () => {
  it("draws a void clause red with bar and tint", () => {
    const s = style("done", "potentially_void");
    expect(s).toMatchObject({ tone: "void", bar: "solid", color: "var(--risk)", visible: true });
    expect(s.tint).toContain("var(--risk)");
    expect(s.label).toBe("risk.void");
    expect(s.pulse || s.retry).toBe(false);
  });

  it("draws a clause to review amber with bar and tint", () => {
    const s = style("done", "uncertain");
    expect(s).toMatchObject({ tone: "review", bar: "solid", color: "var(--review)" });
    expect(s.tint).toContain("var(--review)");
  });

  it("gives a valid clause a faint margin mark only", () => {
    const s = style("done", "valid");
    expect(s).toMatchObject({ tone: "valid", bar: "faint", tint: null, visible: true });
  });

  it("draws nothing for an out-of-scope clause", () => {
    const s = style("out_of_scope");
    expect(s).toMatchObject({ tone: "out", bar: "none", tint: null, visible: false });
  });

  it("shows a queued clause as a dashed grey bar", () => {
    const s = style("queued");
    expect(s).toMatchObject({ tone: "queued", bar: "dashed", tint: null, pulse: false });
    expect(s.color).toBe("var(--muted-foreground)");
  });

  it("pulses a clause being checked", () => {
    expect(style("checking")).toMatchObject({ tone: "checking", pulse: true, retry: false });
  });

  it("offers a retry for a failed clause", () => {
    expect(style("failed")).toMatchObject({
      tone: "failed",
      retry: true,
      pulse: false,
      bar: "solid",
    });
  });

  it("every visible tone has an icon and a label", () => {
    for (const s of [
      style("done", "potentially_void"),
      style("done", "uncertain"),
      style("done", "valid"),
      style("queued"),
      style("checking"),
      style("failed"),
    ]) {
      expect(s.icon).toBeTruthy();
      expect(s.label).toMatch(/^risk\./);
    }
  });

  it("strengthens the tint when emphasised, never for out of scope", () => {
    const voidStyle = style("done", "potentially_void");
    expect(emphasisTint(voidStyle, false)).toBe(voidStyle.tint);
    expect(emphasisTint(voidStyle, true)).toContain("26%");
    expect(emphasisTint(style("done", "valid"), true)).toContain("var(--accent)");
    expect(emphasisTint(style("out_of_scope"), true)).toBeNull();
  });
});

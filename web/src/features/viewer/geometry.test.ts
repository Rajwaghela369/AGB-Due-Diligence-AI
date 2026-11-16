import { describe, expect, it } from "vitest";

import type { Rect } from "../../api/types.gen";
import {
  canvasPixelSize,
  clauseRectToPoints,
  CSS_PER_POINT,
  pageCssSize,
  pointsToCss,
  rectToCssBox,
  rectsByPage,
  textItemMatches,
  unionBox,
  viewportScale,
  zoomIn,
  zoomOut,
  type PageView,
} from "./geometry";

const A4: PageView = [0, 0, 595.28, 841.89];
// A line of "§ 6.1 Unsere Haftung …" as pdfplumber reports it.
const line: Rect = { page: 1, x0: 72, top: 144, x1: 504, bottom: 156 };

describe("rect → CSS box", () => {
  it("maps points to CSS pixels at 100% (96 px per 72 pt)", () => {
    expect(rectToCssBox(line, A4, 1)).toEqual({ left: 96, top: 192, width: 576, height: 16 });
  });

  it("maps at 75%", () => {
    expect(rectToCssBox(line, A4, 0.75)).toEqual({ left: 72, top: 144, width: 432, height: 12 });
  });

  it("maps at 200%", () => {
    expect(rectToCssBox(line, A4, 2)).toEqual({ left: 192, top: 384, width: 1152, height: 32 });
  });

  it("scales linearly: every edge doubles from 100% to 200%", () => {
    const odd: Rect = { page: 1, x0: 71.3, top: 100.45, x1: 300.9, bottom: 111.2 };
    const at100 = rectToCssBox(odd, A4, 1, 4);
    const at200 = rectToCssBox(odd, A4, 2, 4);
    expect(at200.left).toBeCloseTo(at100.left * 2, 0);
    expect(at200.top).toBeCloseTo(at100.top * 2, 0);
    expect(at200.left + at200.width).toBeCloseTo((at100.left + at100.width) * 2, 0);
  });

  it("snaps edges to the device pixel grid", () => {
    const odd: Rect = { page: 1, x0: 10.1, top: 10.1, x1: 20.2, bottom: 20.2 };
    // 10.1 pt * 4/3 = 13.4667 px, 20.2 pt = 26.9333 px.
    expect(rectToCssBox(odd, A4, 1, 1)).toEqual({ left: 13, top: 13, width: 14, height: 14 });
    expect(rectToCssBox(odd, A4, 1, 2)).toEqual({
      left: 13.5,
      top: 13.5,
      width: 13.5,
      height: 13.5,
    });
    const at3 = rectToCssBox(odd, A4, 1, 3);
    expect(at3.left * 3).toBeCloseTo(Math.round(at3.left * 3), 9);
    expect((at3.left + at3.width) * 3).toBeCloseTo(Math.round((at3.left + at3.width) * 3), 9);
  });

  it("keeps adjacent lines gap-free after snapping", () => {
    const first: Rect = { page: 1, x0: 72, top: 100.3, x1: 300, bottom: 112.7 };
    const second: Rect = { page: 1, x0: 72, top: 112.7, x1: 300, bottom: 125.1 };
    for (const zoom of [0.75, 1, 1.1, 2]) {
      for (const dpr of [1, 1.5, 2]) {
        const a = rectToCssBox(first, A4, zoom, dpr);
        const b = rectToCssBox(second, A4, zoom, dpr);
        expect(a.top + a.height).toBe(b.top);
      }
    }
  });

  it("treats a non-positive device pixel ratio as 1", () => {
    expect(rectToCssBox(line, A4, 1, 0)).toEqual(rectToCssBox(line, A4, 1, 1));
  });

  it("normalises inverted rects", () => {
    const inverted: Rect = { page: 1, x0: 504, top: 156, x1: 72, bottom: 144 };
    expect(rectToCssBox(inverted, A4, 1)).toEqual(rectToCssBox(line, A4, 1));
  });

  it("clamps zoom to the 75%–200% range", () => {
    expect(rectToCssBox(line, A4, 0.1)).toEqual(rectToCssBox(line, A4, 0.75));
    expect(rectToCssBox(line, A4, 5)).toEqual(rectToCssBox(line, A4, 2));
  });

  it("accounts for a page whose view box does not start at the origin", () => {
    // MediaBox [10, 20, 605.28, 861.89]: pdfplumber keeps x raw and reports `top` as
    // mediaboxHeight - y, so the line's raw baseline-top y is 841.89 - 144.
    const shifted: PageView = [10, 20, 605.28, 861.89];
    const rect: Rect = { page: 1, x0: 82, top: 144, x1: 514, bottom: 156 };
    expect(clauseRectToPoints(rect, shifted)).toEqual({ x0: 72, top: 164, x1: 504, bottom: 176 });
    // pdf.js puts raw y at view[3] - y = 861.89 - (841.89 - 144) = 164 points from the top.
    const rawY = 841.89 - 144;
    expect(shifted[3] - rawY).toBeCloseTo(164, 9);
  });
});

describe("page size", () => {
  it("matches pdf.js's viewport size for the zoom", () => {
    expect(pageCssSize([0, 0, 612, 792], 1)).toEqual({ width: 816, height: 1056 });
    expect(pageCssSize([0, 0, 612, 792], 0.75)).toEqual({ width: 612, height: 792 });
    expect(pageCssSize([0, 0, 612, 792], 2)).toEqual({ width: 1632, height: 2112 });
    expect(viewportScale(1)).toBe(CSS_PER_POINT);
  });

  it("uses the device pixel ratio for the canvas, capped by area", () => {
    expect(canvasPixelSize({ width: 816, height: 1056 }, 2)).toEqual({
      width: 1632,
      height: 2112,
      ratio: 2,
    });
    const capped = canvasPixelSize({ width: 1632, height: 2112 }, 3, 4_000_000);
    expect(capped.width * capped.height).toBeLessThanOrEqual(4_000_000);
    expect(capped.ratio).toBeLessThan(3);
    expect(capped.width / capped.height).toBeCloseTo(1632 / 2112, 2);
  });
});

describe("zoom steps", () => {
  it("steps between 75% and 200%", () => {
    expect(zoomIn(1)).toBe(1.1);
    expect(zoomOut(1)).toBe(0.9);
    expect(zoomIn(2)).toBe(2);
    expect(zoomOut(0.75)).toBe(0.75);
    expect(zoomIn(1.3)).toBe(1.5);
    expect(zoomOut(1.3)).toBe(1.25);
  });
});

describe("helpers", () => {
  it("unions boxes and groups rects by page", () => {
    expect(
      unionBox([
        { left: 10, top: 20, width: 100, height: 10 },
        { left: 5, top: 30, width: 50, height: 10 },
      ]),
    ).toEqual({ left: 5, top: 20, width: 105, height: 20 });
    expect(unionBox([])).toBeNull();
    const grouped = rectsByPage([{ ...line, page: 2 }, line, { ...line, page: 2, top: 170 }]);
    expect([...grouped.keys()]).toEqual([2, 1]);
    expect(grouped.get(2)).toHaveLength(2);
  });

  it("finds search hits inside pdf.js text items", () => {
    const item = {
      str: "Haftung und haftung",
      transform: [12, 0, 0, 12, 72, 700],
      width: 114,
      height: 12,
    };
    const hits = textItemMatches([item], "HAFTUNG", A4);
    expect(hits).toHaveLength(2);
    expect(hits[0]?.x0).toBeCloseTo(72, 6);
    expect(hits[0]?.x1).toBeCloseTo(72 + (114 * 7) / 19, 6);
    expect(hits[1]?.x0).toBeCloseTo(72 + (114 * 12) / 19, 6);
    // Baseline 700 pt from the bottom is 141.89 pt from the top; the box straddles it.
    const baseline = 841.89 - 700;
    expect(hits[0]!.top).toBeLessThan(baseline);
    expect(hits[0]!.bottom).toBeGreaterThan(baseline);
    expect(textItemMatches([item], "  ", A4)).toEqual([]);
    expect(pointsToCss(hits[0]!, 1).left).toBe(96);
  });
});

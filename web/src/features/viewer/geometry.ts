// Where a clause's rectangles land on screen. Kept pure so the one thing that must be
// exactly right in the viewer, highlights sitting on the clause text at every zoom, is
// unit-tested without a browser or pdf.js.
//
// Coordinate systems:
// - Clause rects come from pdfplumber: PDF points, origin at the top-left of the MediaBox,
//   `top`/`bottom` measured downwards. pdfplumber keeps x in raw user space and measures
//   `top` as `mediaboxHeight - y`, so for a MediaBox [x0, y0, x1, y1] a rect's raw y is
//   `y1 - y0 - top`.
// - pdf.js renders the page's `view` box ([x0, y0, x1, y1], raw user space, y up) and maps
//   raw (x, y) to CSS ((x - view[0]) * scale, (view[3] - y) * scale).
// - CSS pixels: pdf.js's own viewer treats 100% as 96 CSS px per 72 pt, so we do too.
//
// Assumes CropBox = MediaBox and no /Rotate, which holds for the PDFs we ingest (pdfplumber
// reads the MediaBox; Gotenberg output and typical AGB exports are unrotated and uncropped).
import type { Rect } from "../../api/types.gen";

/** CSS pixels per PDF point at 100% zoom (96 dpi / 72 dpi). */
export const CSS_PER_POINT = 96 / 72;

export const ZOOM_MIN = 0.75;
export const ZOOM_MAX = 2;
export const ZOOM_STEPS = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2] as const;

/** pdf.js `page.view`: [x0, y0, x1, y1] in raw PDF user space. */
export type PageView = readonly [number, number, number, number];

/** A box in points relative to the rendered page's top-left corner. */
export interface PointBox {
  x0: number;
  top: number;
  x1: number;
  bottom: number;
}

export interface CssBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

export function clampZoom(zoom: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));
}

export function zoomIn(zoom: number): number {
  return ZOOM_STEPS.find((step) => step > zoom + 1e-9) ?? ZOOM_MAX;
}

export function zoomOut(zoom: number): number {
  return [...ZOOM_STEPS].reverse().find((step) => step < zoom - 1e-9) ?? ZOOM_MIN;
}

/** pdf.js viewport scale for a zoom level. */
export function viewportScale(zoom: number): number {
  return clampZoom(zoom) * CSS_PER_POINT;
}

/** The page's size in CSS pixels, unsnapped, so it matches pdf.js's viewport exactly. */
export function pageCssSize(view: PageView, zoom: number): { width: number; height: number } {
  const scale = viewportScale(zoom);
  return { width: (view[2] - view[0]) * scale, height: (view[3] - view[1]) * scale };
}

/** A pdfplumber rect in points relative to the rendered page's top-left corner. */
export function clauseRectToPoints(rect: Rect, view: PageView): PointBox {
  // Raw y of a pdfplumber `top` is (mediaboxHeight - top); with MediaBox = view that is
  // view[3] - view[1] - top, and pdf.js puts it at view[3] - rawY = top + view[1].
  return {
    x0: rect.x0 - view[0],
    top: rect.top + view[1],
    x1: rect.x1 - view[0],
    bottom: rect.bottom + view[1],
  };
}

/**
 * Points to CSS pixels at a zoom, with each edge snapped to the device pixel grid so a
 * highlight's edges are crisp and line up with the canvas, which pdf.js renders at the
 * device pixel ratio. Edges are snapped (not the size) so adjacent lines never overlap
 * or leave a hairline gap.
 */
export function pointsToCss(box: PointBox, zoom: number, devicePixelRatio = 1): CssBox {
  const scale = viewportScale(zoom);
  const dpr = devicePixelRatio > 0 ? devicePixelRatio : 1;
  const snap = (value: number) => Math.round(value * scale * dpr) / dpr;
  const left = snap(Math.min(box.x0, box.x1));
  const right = snap(Math.max(box.x0, box.x1));
  const top = snap(Math.min(box.top, box.bottom));
  const bottom = snap(Math.max(box.top, box.bottom));
  return { left, top, width: right - left, height: bottom - top };
}

/** The full transform: one clause rect to its CSS box on the rendered page. */
export function rectToCssBox(
  rect: Rect,
  view: PageView,
  zoom: number,
  devicePixelRatio = 1,
): CssBox {
  return pointsToCss(clauseRectToPoints(rect, view), zoom, devicePixelRatio);
}

/** The smallest box around several boxes (the margin bar spans a clause's lines). */
export function unionBox(boxes: CssBox[]): CssBox | null {
  if (boxes.length === 0) return null;
  const left = Math.min(...boxes.map((b) => b.left));
  const top = Math.min(...boxes.map((b) => b.top));
  const right = Math.max(...boxes.map((b) => b.left + b.width));
  const bottom = Math.max(...boxes.map((b) => b.top + b.height));
  return { left, top, width: right - left, height: bottom - top };
}

/** A clause's rects grouped by 1-based page number, in their original order. */
export function rectsByPage(rects: Rect[]): Map<number, Rect[]> {
  const pages = new Map<number, Rect[]>();
  for (const rect of rects) {
    const list = pages.get(rect.page);
    if (list) list.push(rect);
    else pages.set(rect.page, [rect]);
  }
  return pages;
}

/**
 * Canvas backing-store size for a page: CSS size times the device pixel ratio, reduced
 * proportionally when it would exceed `maxPixels` (browsers refuse larger canvases).
 * `ratio` is the effective output scale to hand pdf.js as its transform.
 */
export function canvasPixelSize(
  css: { width: number; height: number },
  devicePixelRatio: number,
  maxPixels = 16_000_000,
): { width: number; height: number; ratio: number } {
  let ratio = devicePixelRatio > 0 ? devicePixelRatio : 1;
  const area = css.width * css.height * ratio * ratio;
  if (area > maxPixels) ratio *= Math.sqrt(maxPixels / area);
  return {
    width: Math.floor(css.width * ratio),
    height: Math.floor(css.height * ratio),
    ratio,
  };
}

/** The part of a pdf.js text item the search needs. */
export interface TextItemLike {
  str: string;
  /** [a, b, c, d, e, f]: e/f are the baseline origin in raw user space. */
  transform: number[];
  width: number;
  height: number;
}

/**
 * Boxes (points, page top-left origin) for each case-insensitive match of `query` inside
 * single pdf.js text items. A match's horizontal extent is interpolated from the item's
 * width; the box spans roughly ascent to descent around the baseline.
 */
export function textItemMatches(items: TextItemLike[], query: string, view: PageView): PointBox[] {
  const needle = query.trim().toLowerCase();
  if (needle.length === 0) return [];
  const boxes: PointBox[] = [];
  for (const item of items) {
    const haystack = item.str.toLowerCase();
    // Case folding that changes length (rare) would misplace the box; skip such items.
    if (haystack.length !== item.str.length || haystack.length === 0) continue;
    const e = item.transform[4] ?? 0;
    const f = item.transform[5] ?? 0;
    const fontHeight = item.height || Math.abs(item.transform[3] ?? 0) || 10;
    const baseline = view[3] - f;
    let from = haystack.indexOf(needle);
    while (from !== -1) {
      const start = e - view[0] + (item.width * from) / haystack.length;
      const end = e - view[0] + (item.width * (from + needle.length)) / haystack.length;
      boxes.push({
        x0: start,
        x1: end,
        top: baseline - fontHeight * 0.85,
        bottom: baseline + fontHeight * 0.25,
      });
      from = haystack.indexOf(needle, from + needle.length);
    }
  }
  return boxes;
}

// The only module that touches pdf.js. It is loaded on demand so the library (and its
// worker) stays out of the main bundle, and component tests replace this module instead of
// pdf.js itself.
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

import type { PageView, TextItemLike } from "./geometry";

export interface PdfRenderTask {
  promise: Promise<unknown>;
  cancel(): void;
}

export interface PdfPageLike {
  view: PageView;
  render(params: {
    canvas: HTMLCanvasElement;
    scale: number;
    /** Output scale for the device pixel ratio (pdf.js `transform`). */
    ratio: number;
  }): PdfRenderTask;
  textItems(): Promise<TextItemLike[]>;
}

export interface PdfDocumentLike {
  numPages: number;
  getPage(pageNumber: number): Promise<PdfPageLike>;
  destroy(): void;
}

export async function openPdf(data: ArrayBuffer): Promise<PdfDocumentLike> {
  const pdfjs = await import("pdfjs-dist");
  pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;
  const task = pdfjs.getDocument({ data: new Uint8Array(data) });
  const doc = await task.promise;
  return {
    numPages: doc.numPages,
    async getPage(pageNumber) {
      const page = await doc.getPage(pageNumber);
      const [x0 = 0, y0 = 0, x1 = 0, y1 = 0] = page.view;
      return {
        view: [x0, y0, x1, y1],
        render({ canvas, scale, ratio }) {
          // Rotation 0: the highlight geometry assumes unrotated pages (see geometry.ts).
          const viewport = page.getViewport({ scale, rotation: 0 });
          const task = page.render({
            canvas,
            viewport,
            ...(ratio === 1 ? {} : { transform: [ratio, 0, 0, ratio, 0, 0] }),
          });
          return { promise: task.promise, cancel: () => task.cancel() };
        },
        async textItems() {
          const content = await page.getTextContent();
          return content.items.flatMap((item) =>
            "str" in item
              ? [
                  {
                    str: item.str,
                    transform: item.transform,
                    width: item.width,
                    height: item.height,
                  },
                ]
              : [],
          );
        },
      };
    },
    destroy() {
      void task.destroy();
    },
  };
}

// The PDF as sent, page by page. Every page's box is reserved at its real size as soon as
// pdf.js reports it (nothing jumps as pages render), canvases are drawn only near the
// viewport, and the highlight overlays are always present so the document can scroll to
// any clause before its page has been painted.
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import type { ReviewFinding } from "../review/use-review";
import {
  canvasPixelSize,
  pageCssSize,
  textItemMatches,
  viewportScale,
  type PageView,
  type PointBox,
} from "./geometry";
import { useNearViewport } from "./hooks";
import { clausesByPage, hitOffsets, type ClauseInteraction } from "./layout";
import { PageHighlights } from "./page-highlights";
import { openPdf, type PdfDocumentLike, type PdfPageLike } from "./pdf";

/** A4 in points, the placeholder size until pdf.js reports a page's real one. */
const FALLBACK_VIEW: PageView = [0, 0, 595.28, 841.89];

export function PdfView(props: {
  documentId: string;
  findings: ReviewFinding[];
  zoom: number;
  dpr: number;
  query: string;
  activeHit: number;
  interaction: ClauseInteraction;
  scrollRoot: RefObject<HTMLDivElement | null>;
  onReady: (width: number) => void;
  onHitCount: (count: number) => void;
}) {
  const { t } = useTranslation();
  const { documentId, onReady, onHitCount } = props;
  const file = useQuery({
    queryKey: ["documents", documentId, "file"],
    queryFn: () => api.documents.file(documentId),
    staleTime: Infinity,
  });
  const [doc, setDoc] = useState<PdfDocumentLike | null>(null);
  const [pages, setPages] = useState<Array<PdfPageLike | null>>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const blob = file.data;
    if (!blob) return;
    let cancelled = false;
    let opened: PdfDocumentLike | null = null;
    void (async () => {
      try {
        opened = await openPdf(await blob.arrayBuffer());
        if (cancelled) return opened.destroy();
        const count = opened.numPages;
        setDoc(opened);
        setPages(Array.from({ length: count }, () => null));
        // Page 1 first so it paints at once; the rest only report their sizes.
        const loaded = opened;
        const first = await loaded.getPage(1);
        if (cancelled) return;
        setPages((current) => current.map((page, index) => (index === 0 ? first : page)));
        await Promise.all(
          Array.from({ length: count - 1 }, async (_, index) => {
            const page = await loaded.getPage(index + 2);
            if (!cancelled) {
              setPages((current) => current.map((p, i) => (i === index + 1 ? page : p)));
            }
          }),
        );
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
      opened?.destroy();
      setDoc(null);
      setPages([]);
    };
  }, [file.data]);

  const width = pages.reduce(
    (max, page) => (page ? Math.max(max, pageCssSize(page.view, 1).width) : max),
    0,
  );
  useEffect(() => {
    if (width) onReady(width);
  }, [width, onReady]);

  const byPage = useMemo(() => clausesByPage(props.findings), [props.findings]);
  const hitsByPage = usePdfSearch(doc, pages, props.query);
  const hitCount = hitsByPage.reduce((sum, hits) => sum + hits.length, 0);
  useEffect(() => onHitCount(hitCount), [hitCount, onHitCount]);

  if (file.isError || failed) {
    return (
      <p role="alert" className="p-6 text-sm">
        {t("viewer.loadError")}
      </p>
    );
  }
  if (pages.length === 0) {
    return (
      <p aria-busy="true" className="p-6 text-sm text-muted-foreground">
        {t("viewer.loading")}
      </p>
    );
  }

  const knownView = pages.find((page) => page !== null)?.view ?? FALLBACK_VIEW;
  const offsets = hitOffsets(hitsByPage);
  return (
    <div className="flex w-max min-w-full flex-col items-center gap-4 px-4 py-5">
      {pages.map((page, index) => {
        const pageNumber = index + 1;
        const hits = hitsByPage[index] ?? [];
        return (
          <PdfPage
            key={pageNumber}
            pageNumber={pageNumber}
            pageCount={pages.length}
            page={page}
            view={page?.view ?? knownView}
            zoom={props.zoom}
            dpr={props.dpr}
            scrollRoot={props.scrollRoot}
          >
            <PageHighlights
              view={page?.view ?? knownView}
              zoom={props.zoom}
              dpr={props.dpr}
              clauses={byPage.get(pageNumber) ?? []}
              interaction={props.interaction}
              hits={hits}
              hitOffset={offsets[index] ?? 0}
              activeHit={props.activeHit}
            />
          </PdfPage>
        );
      })}
    </div>
  );
}

function PdfPage(props: {
  pageNumber: number;
  pageCount: number;
  page: PdfPageLike | null;
  view: PageView;
  zoom: number;
  dpr: number;
  scrollRoot: RefObject<HTMLDivElement | null>;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const { page, zoom, dpr } = props;
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const near = useNearViewport(frame, props.scrollRoot);
  const size = pageCssSize(props.view, zoom);
  // Which render settings the canvas currently shows; hidden until it matches, so a page
  // never flashes a stale or blank bitmap at the wrong size.
  const paintKey = `${zoom}@${dpr}`;
  const [paintedKey, setPaintedKey] = useState<string | null>(null);
  const painted = near && paintedKey === paintKey;

  useEffect(() => {
    const target = canvas.current;
    if (!target || !page) return;
    if (!near) {
      // Release far-away pages' pixels; they repaint when they come near again.
      target.width = 0;
      target.height = 0;
      return;
    }
    const css = pageCssSize(page.view, zoom);
    const pixels = canvasPixelSize(css, dpr);
    target.width = pixels.width;
    target.height = pixels.height;
    const task = page.render({ canvas: target, scale: viewportScale(zoom), ratio: pixels.ratio });
    let live = true;
    task.promise.then(
      () => live && setPaintedKey(`${zoom}@${dpr}`),
      () => undefined, // cancelled by a newer zoom or by scrolling away
    );
    return () => {
      live = false;
      task.cancel();
    };
  }, [page, near, zoom, dpr]);

  return (
    <div
      ref={frame}
      data-page={props.pageNumber}
      role="region"
      aria-label={t("viewer.page", { page: props.pageNumber, total: props.pageCount })}
      className="relative shrink-0 bg-white shadow-sm ring-1 ring-border"
      style={{ width: size.width, height: size.height }}
    >
      <canvas
        ref={canvas}
        aria-hidden="true"
        className="absolute inset-0 transition-opacity motion-reduce:transition-none"
        style={{ width: size.width, height: size.height, opacity: painted ? 1 : 0 }}
      />
      {props.children}
    </div>
  );
}

/** Search hits per page; text content is fetched once per page and only when searching. */
function usePdfSearch(
  doc: PdfDocumentLike | null,
  pages: Array<PdfPageLike | null>,
  query: string,
): PointBox[][] {
  const cache = useRef(
    new Map<PdfPageLike, Promise<Awaited<ReturnType<PdfPageLike["textItems"]>>>>(),
  );
  const [found, setFound] = useState<{ needle: string; hits: PointBox[][] } | null>(null);
  const needle = query.trim();
  const ready = pages.every((page) => page !== null);
  const active = doc !== null && ready && needle.length >= 2;

  useEffect(() => {
    if (!active) return;
    let cancelled = false;
    void Promise.all(
      pages.map(async (page) => {
        if (!page) return [];
        let items = cache.current.get(page);
        if (!items) {
          items = page.textItems();
          cache.current.set(page, items);
        }
        try {
          return textItemMatches(await items, needle, page.view);
        } catch {
          return [];
        }
      }),
    ).then((result) => {
      if (!cancelled) setFound({ needle, hits: result });
    });
    return () => {
      cancelled = true;
    };
  }, [active, pages, needle]);

  return active && found?.needle === needle ? found.hits : [];
}

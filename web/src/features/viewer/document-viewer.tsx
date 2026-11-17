// The contract rendered as sent, with every finding drawn on it (Phase 6 plan, 6.C).
//
// PDF and DOCX (converted to PDF at ingest) render through pdf.js; pasted text renders in
// the contract serif. Both draw the same highlight model and share selection with the rest
// of the workspace through `useReviewUi`: hover and click write to it, and a selection made
// anywhere scrolls the clause into view here.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import type { SourceType } from "../../api/types.gen";
import { useReviewUi } from "../review/store";
import type { ReviewData } from "../review/use-review";
import { useDevicePixelRatio, useReducedMotion } from "./hooks";
import { inferSourceType, type ClauseInteraction } from "./layout";
import { PdfView } from "./pdf-view";
import { StepBar } from "./step-bar";
import { TextView } from "./text-view";
import { ViewerToolbar } from "./toolbar";

export function DocumentViewer(props: { documentId: string; review: ReviewData }) {
  const { documentId, review } = props;
  const { t } = useTranslation();
  const sourceType: SourceType | null =
    review.document?.document.source_type ?? inferSourceType(review.findings);
  // Pasted text is sliced from the exact source so the gaps between clauses stay in place.
  const source = useQuery({
    queryKey: ["documents", documentId, "text"],
    queryFn: () => api.documents.text(documentId),
    enabled: sourceType === "text",
    staleTime: Infinity,
  });

  const selected = useReviewUi((s) => s.selected);
  const hovered = useReviewUi((s) => s.hovered);
  const select = useReviewUi((s) => s.select);
  const hover = useReviewUi((s) => s.hover);
  const reducedMotion = useReducedMotion();
  const dpr = useDevicePixelRatio();

  const [zoom, setZoom] = useState(1);
  const [query, setQuery] = useState("");
  const [hitCount, setHitCount] = useState(0);
  const [activeHit, setActiveHit] = useState(0);
  const [ready, setReady] = useState(false);
  const scrollRoot = useRef<HTMLDivElement>(null);
  const zoomAnchor = useRef<number | null>(null);

  const reviewId = review.review?.review_id;
  const refresh = review.refresh;
  const [retrying, setRetrying] = useState<ReadonlySet<number>>(new Set());
  const retryMutation = useMutation({
    mutationFn: (position: number) => api.reviews.retryClause(reviewId as string, position),
    onMutate: (position) => setRetrying((current) => new Set(current).add(position)),
    onSettled: (_data, _error, position) => {
      setRetrying((current) => {
        const remaining = new Set(current);
        remaining.delete(position);
        return remaining;
      });
      // A finished review has no open stream; refreshing reconnects it for the retry's events.
      void refresh();
    },
  });
  const retry = retryMutation.mutate;

  const interaction = useMemo<ClauseInteraction>(
    () => ({
      hovered,
      selected,
      hover,
      select,
      retry: (position) => {
        if (reviewId) retry(position);
      },
      retrying,
    }),
    [hovered, selected, hover, select, retry, reviewId, retrying],
  );

  const scrollTo = useCallback(
    (selector: string) => {
      const target = scrollRoot.current?.querySelector(selector);
      target?.scrollIntoView({ block: "center", behavior: reducedMotion ? "auto" : "smooth" });
    },
    [reducedMotion],
  );

  // A selection from anywhere (panel, table, J/K, ◀ ▶) brings the clause into view; also
  // once the first page has a layout, for a selection made before the document was ready.
  const isReady = ready || sourceType === "text";
  useEffect(() => {
    if (selected !== null && isReady) scrollTo(`[data-clause-anchor="${selected}"]`);
  }, [selected, isReady, scrollTo]);

  useEffect(() => {
    if (hitCount > 0) scrollTo(`[data-hit-index="${Math.min(activeHit, hitCount - 1)}"]`);
  }, [activeHit, hitCount, scrollTo]);

  const changeQuery = useCallback((value: string) => {
    setQuery(value);
    setActiveHit(0);
  }, []);
  // Esc anywhere clears an active search; the drawer closes on its own Esc.
  useHotkeys("esc", () => changeQuery(""), { enabled: query !== "", enableOnFormTags: true }, [
    query,
    changeQuery,
  ]);
  const stepHit = useCallback(
    (delta: 1 | -1) => {
      if (hitCount === 0) return;
      setActiveHit((current) => (current + delta + hitCount) % hitCount);
    },
    [hitCount],
  );

  // Keep the point at the centre of the view in place when the zoom changes.
  const changeZoom = useCallback((next: number) => {
    const root = scrollRoot.current;
    if (root && root.scrollHeight > 0) {
      zoomAnchor.current = (root.scrollTop + root.clientHeight / 2) / root.scrollHeight;
    }
    setZoom(next);
  }, []);
  useLayoutEffect(() => {
    const root = scrollRoot.current;
    const anchor = zoomAnchor.current;
    zoomAnchor.current = null;
    if (root && anchor !== null) {
      root.scrollTop = anchor * root.scrollHeight - root.clientHeight / 2;
    }
  }, [zoom]);

  const onReady = useCallback(() => setReady(true), []);
  const showStepBar =
    review.review !== null && (!review.isFinal || review.review.status === "awaiting_decisions");

  return (
    <div
      data-testid="document-viewer"
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-md border border-border bg-surface"
    >
      <ViewerToolbar
        query={query}
        onQuery={changeQuery}
        hitCount={hitCount}
        activeHit={Math.min(activeHit, Math.max(hitCount - 1, 0))}
        onHit={stepHit}
        zoom={zoom}
        onZoom={changeZoom}
      />
      {showStepBar && review.review && (
        <StepBar review={review.review} progress={review.progress} />
      )}
      <div
        ref={scrollRoot}
        data-testid="viewer-scroll"
        className="h-[calc(100vh-16rem)] min-h-96 overflow-auto bg-muted/60"
      >
        {sourceType === null ? (
          review.error ? (
            <p role="alert" className="p-6 text-sm">
              {t("viewer.loadError")}
            </p>
          ) : (
            <p aria-busy="true" className="p-6 text-sm text-muted-foreground">
              {t("viewer.loading")}
            </p>
          )
        ) : sourceType === "text" ? (
          <div className="p-6">
            <TextView
              findings={review.findings}
              source={source.data}
              zoom={zoom}
              query={query}
              activeHit={activeHit}
              interaction={interaction}
              onHitCount={setHitCount}
            />
          </div>
        ) : (
          <PdfView
            documentId={documentId}
            findings={review.findings}
            zoom={zoom}
            dpr={dpr}
            query={query}
            activeHit={activeHit}
            interaction={interaction}
            scrollRoot={scrollRoot}
            onReady={onReady}
            onHitCount={setHitCount}
          />
        )}
      </div>
    </div>
  );
}

// The review workspace layout (Phase 6 plan, "Review workspace"). Owned by the
// orchestrator: each panel lives in its own feature folder and receives the same
// `ReviewData`, and all of them share selection through `useReviewUi`.
//
// The workspace fills the viewport below the header and the page itself never scrolls: the
// document and the right column scroll internally. The right column is master/detail: the
// findings list, or the drawer for the selected clause. Below 1024px only one of the two
// areas shows, switched by a pair of tabs.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useParams } from "react-router-dom";

import type { ClauseVerdict } from "../../api/types.gen";
import { cn } from "../../lib/utils";
import { FindingDrawer } from "../findings/finding-drawer";
import { FindingsPanel } from "../findings/findings-panel";
import { FindingsTable } from "../table/findings-table";
import { DocumentViewer } from "../viewer/document-viewer";
import { WorkspaceHotkeys } from "./hotkeys";
import { riskStyle } from "./risk";
import { useReviewUi } from "./store";
import { useReview, type ReviewFinding } from "./use-review";

const probe = (verdict: string): Pick<ReviewFinding, "status" | "verdict"> => ({
  status: "done",
  verdict: { verdict } as ClauseVerdict,
});

// The same icon, label and colour as the home page's chips, from `riskStyle`.
const SUMMARY = [
  { key: "void", count: "potentially_void", style: riskStyle(probe("potentially_void")) },
  { key: "review", count: "uncertain", style: riskStyle(probe("uncertain")) },
  { key: "valid", count: "valid", style: riskStyle(probe("valid")) },
  {
    key: "out",
    count: "out_of_scope",
    style: riskStyle({ status: "out_of_scope", verdict: null }),
  },
] as const;

const tabClass = cn(
  "rounded-md border border-border px-3 py-1 text-sm hover:bg-muted",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  "aria-selected:border-foreground aria-selected:bg-foreground aria-selected:text-background",
  "aria-selected:font-medium",
);

const PANES = ["document", "findings"] as const;
type Pane = (typeof PANES)[number];

/** Height from the top of the workspace to the bottom of the viewport, minus the page padding. */
function useFillHeight(ref: React.RefObject<HTMLElement | null>): number | undefined {
  const [height, setHeight] = useState<number>();
  useLayoutEffect(() => {
    const measure = () => {
      const el = ref.current;
      if (!el) return;
      const bottom = parseFloat(getComputedStyle(el.parentElement ?? el).paddingBottom) || 0;
      setHeight(Math.max(window.innerHeight - el.getBoundingClientRect().top - bottom, 320));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [ref]);
  return height;
}

export function ReviewWorkspace() {
  const { t } = useTranslation();
  const { id } = useParams();
  // Set by the upload page when the same file was reviewed before: no new review ran.
  const alreadyReviewed = Boolean(
    (useLocation().state as { alreadyReviewed?: boolean } | null)?.alreadyReviewed,
  );
  const review = useReview(id);
  const view = useReviewUi((s) => s.view);
  const setView = useReviewUi((s) => s.setView);
  const reset = useReviewUi((s) => s.reset);

  const selected = useReviewUi((s) => s.selected);
  const [pane, setPane] = useState<Pane>("document");
  const fillRef = useRef<HTMLDivElement>(null);
  const fillHeight = useFillHeight(fillRef);

  useEffect(() => reset, [id, reset]);
  // On a narrow screen, picking a finding jumps to the document so the clause is in view.
  const [seenSelected, setSeenSelected] = useState(selected);
  if (selected !== seenSelected) {
    setSeenSelected(selected);
    if (selected !== null) setPane("document");
  }

  if (review.error) {
    return <p role="alert">{t("workspace.loadError")}</p>;
  }
  if (review.isLoading || review.review === null) {
    return <p aria-busy="true">{t("workspace.loading")}</p>;
  }
  const counts = review.review.counts;

  return (
    <div
      ref={fillRef}
      style={fillHeight === undefined ? undefined : { height: fillHeight }}
      className={cn(
        "workspace-fill flex h-[calc(100dvh-14rem)] flex-col gap-3 overflow-clip",
        // Wide screens: break out of the page's max width so the right column can grow.
        "min-[1600px]:relative min-[1600px]:left-1/2 min-[1600px]:w-[calc(100vw-3rem)]",
        "min-[1600px]:max-w-[120rem] min-[1600px]:-translate-x-1/2",
      )}
    >
      <div role="tablist" aria-label={t("workspace.panes")} className="flex gap-2 lg:hidden">
        {PANES.map((p) => (
          <button
            key={p}
            role="tab"
            type="button"
            aria-selected={pane === p}
            onClick={() => setPane(p)}
            className={tabClass}
          >
            {t(p === "document" ? "workspace.document" : "findings.title")}
          </button>
        ))}
      </div>
      <div className="workspace-grid grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <section
          className={cn("min-h-0 flex-col", pane === "document" ? "flex" : "hidden lg:flex")}
          aria-label={t("workspace.document")}
        >
          {alreadyReviewed && (
            <p role="status" className="mb-2 text-sm text-muted-foreground">
              {t("workspace.alreadyReviewed")}
            </p>
          )}
          <ul className="mb-2 flex flex-wrap items-center gap-2 text-sm" data-testid="risk-summary">
            {SUMMARY.map(({ key, count, style }) => {
              const Icon = style.icon;
              const muted = style.tone === "out";
              return (
                <li
                  key={key}
                  className="flex items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-0.5"
                  style={muted ? undefined : { color: style.color, borderColor: style.color }}
                >
                  <Icon aria-hidden className="size-4 shrink-0" />
                  <span>{t(`workspace.counts.${key}`, { count: counts?.[count] ?? 0 })}</span>
                </li>
              );
            })}
          </ul>
          <div className="mb-2 flex items-center gap-2">
            <div role="tablist" aria-label={t("workspace.views")} className="flex gap-2">
              <button
                role="tab"
                type="button"
                aria-selected={view === "document"}
                onClick={() => setView("document")}
                className={tabClass}
              >
                {t("workspace.viewDocument")}
              </button>
              <button
                role="tab"
                type="button"
                aria-selected={view === "table"}
                onClick={() => setView("table")}
                className={tabClass}
              >
                {t("workspace.viewTable")}
              </button>
            </div>
            <div className="ml-auto">
              <WorkspaceHotkeys />
            </div>
          </div>
          {view === "document" ? (
            <DocumentViewer documentId={review.review.document_id} review={review} />
          ) : (
            <FindingsTable review={review} />
          )}
        </section>
        <div className={cn("min-h-0 flex-col", pane === "findings" ? "flex" : "hidden lg:flex")}>
          <FindingsPanel review={review} />
          <FindingDrawer review={review} />
        </div>
      </div>
    </div>
  );
}

// The review workspace layout (Phase 6 plan, "Review workspace"). Owned by the
// orchestrator: each panel lives in its own feature folder and receives the same
// `ReviewData`, and all of them share selection through `useReviewUi`.
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useParams } from "react-router-dom";

import { FindingDrawer } from "../findings/finding-drawer";
import { FindingsPanel } from "../findings/findings-panel";
import { FindingsTable } from "../table/findings-table";
import { DocumentViewer } from "../viewer/document-viewer";
import { useReviewUi } from "./store";
import { useReview } from "./use-review";

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

  useEffect(() => reset, [id, reset]);

  if (review.error) {
    return <p role="alert">{t("workspace.loadError")}</p>;
  }
  if (review.isLoading || review.review === null) {
    return <p aria-busy="true">{t("workspace.loading")}</p>;
  }
  const counts = review.review.counts;

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
      <section className="flex min-h-0 flex-col" aria-label={t("workspace.document")}>
        {alreadyReviewed && (
          <p role="status" className="mb-2 text-sm text-muted-foreground">
            {t("workspace.alreadyReviewed")}
          </p>
        )}
        <div className="mb-2 flex items-center gap-4 text-sm" data-testid="risk-summary">
          <span>{t("workspace.counts.void", { count: counts?.potentially_void ?? 0 })}</span>
          <span>{t("workspace.counts.review", { count: counts?.uncertain ?? 0 })}</span>
          <span>{t("workspace.counts.valid", { count: counts?.valid ?? 0 })}</span>
          <span>{t("workspace.counts.out", { count: counts?.out_of_scope ?? 0 })}</span>
        </div>
        <div role="tablist" className="mb-2 flex gap-2">
          <button
            role="tab"
            aria-selected={view === "document"}
            onClick={() => setView("document")}
          >
            {t("workspace.viewDocument")}
          </button>
          <button role="tab" aria-selected={view === "table"} onClick={() => setView("table")}>
            {t("workspace.viewTable")}
          </button>
        </div>
        {view === "document" ? (
          <DocumentViewer documentId={review.review.document_id} review={review} />
        ) : (
          <FindingsTable review={review} />
        )}
      </section>
      <FindingsPanel review={review} />
      <FindingDrawer review={review} />
    </div>
  );
}

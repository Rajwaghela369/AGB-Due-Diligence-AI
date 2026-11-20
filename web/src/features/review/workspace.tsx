import { useEffect, useState } from "react";
import { Download, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useLocation, useParams } from "react-router-dom";

import type { ClauseVerdict } from "../../api/types.gen";
import { cn } from "../../lib/utils";
import { DecisionProgress } from "../decisions/review-progress";
import { ReviewCost } from "../cost/review-cost";
import { ReviewerControl, ReviewerDialogHost } from "../decisions/reviewer-control";
import { FindingDrawer } from "../findings/finding-drawer";
import { FindingsPanel } from "../findings/findings-panel";
import { ExportDialog } from "../summary/export-dialog";
import { FindingsTable } from "../table/findings-table";
import { DocumentViewer } from "../viewer/document-viewer";
import { clauseTitle } from "./clause-title";
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

type Pane = "document" | "findings" | "decision";

export function ReviewWorkspace() {
  const { id } = useParams();
  return <Desk key={id} id={id} />;
}

function Desk({ id }: { id: string | undefined }) {
  const { t } = useTranslation();
  const review = useReview(id);
  const alreadyReviewed = Boolean(useLocation().state?.alreadyReviewed);
  const view = useReviewUi((s) => s.view);
  const setView = useReviewUi((s) => s.setView);
  const selected = useReviewUi((s) => s.selected);
  const select = useReviewUi((s) => s.select);
  const [pane, setPane] = useState<Pane>("document");
  useEffect(() => {
    const unsubscribe = useReviewUi.subscribe((s, prev) => {
      if (s.decisionRequest && s.decisionRequest !== prev.decisionRequest) setPane("decision");
      else if (s.selected !== prev.selected && s.selected !== null) setPane("document");
    });
    return () => {
      unsubscribe();
      useReviewUi.getState().reset();
    };
  }, []);
  if (review.error) return <p role="alert">{t("workspace.loadError")}</p>;
  if (!review.review || review.isLoading) return <p aria-busy="true">{t("workspace.loading")}</p>;
  const record = review.review;
  const summary = review.summary;
  const filename =
    review.document?.document.filename ?? summary?.filename ?? t("workspace.document");
  const finding = selected === null ? null : review.byPosition.get(selected);
  const open = review.findings.filter(
    (f) =>
      f.position !== selected &&
      f.findingStatus === "open" &&
      (f.verdict?.verdict === "potentially_void" || f.verdict?.verdict === "uncertain"),
  );
  const nextOpen = open.find((f) => f.position > (selected ?? -1)) ?? open[0];
  const tabs = (["document", "table"] as const).map((v) => (
    <button
      key={v}
      type="button"
      role="tab"
      aria-selected={view === v}
      className={tabClass}
      onClick={() => setView(v)}
    >
      {t(v === "document" ? "workspace.viewDocument" : "workspace.viewTable")}
    </button>
  ));
  return (
    <div className="review-workspace workspace-fill" data-pane={pane}>
      <ReviewerDialogHost />
      <header className="desk-heading">
        <div className="min-w-0">
          <p className="desk-label">{t("pages.review.title")}</p>
          <h1 title={filename}>
            <FileText aria-hidden size={21} />
            <span className="truncate">{filename}</span>
          </h1>
        </div>
        {summary && (
          <ExportDialog reviewId={record.review_id} defaultDraft={summary.draft}>
            <Download aria-hidden size={16} />
            {t("export.action")}
          </ExportDialog>
        )}
      </header>
      {alreadyReviewed && (
        <p role="status" className="text-sm">
          {t("workspace.alreadyReviewed")}
        </p>
      )}
      <nav className="desk-mobile" role="tablist" aria-label={t("workspace.panes")}>
        {(["findings", "document", "decision"] as const).map((p) => (
          <button
            role="tab"
            type="button"
            key={p}
            aria-selected={p === pane}
            className={tabClass}
            onClick={() => setPane(p)}
          >
            {t(
              p === "findings"
                ? "findings.title"
                : p === "document"
                  ? "workspace.document"
                  : "decision.title",
            )}
          </button>
        ))}
      </nav>
      <div className="workspace-grid">
        <div className="desk-queue flex">
          <FindingsPanel
            review={review}
            onOpen={() => {
              setView("document");
              setPane("document");
            }}
          />
        </div>
        <section className="desk-paper" aria-label={t("workspace.document")}>
          <div className="desk-views" role="tablist" aria-label={t("workspace.views")}>
            {tabs}
          </div>
          {view === "document" ? (
            <DocumentViewer
              key={record.document_id}
              documentId={record.document_id}
              review={review}
            />
          ) : (
            <FindingsTable review={review} />
          )}
        </section>
        <aside className="desk-right" aria-label={t("pages.review.title")}>
          <section className="desk-inspector" aria-label={t("decision.title")}>
            <FindingDrawer review={review} onBack={() => setPane("findings")} />
            {!finding && (
              <div className="desk-prompt">
                <h2>{t("decision.title")}</h2>
                {nextOpen ? (
                  <>
                    <p className="font-medium">{clauseTitle(nextOpen.clause)}</p>
                    <p className="line-clamp-3 text-sm leading-relaxed text-muted-foreground">
                      {nextOpen.clause.text}
                    </p>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">{t("workspace.selectFinding")}</p>
                )}
              </div>
            )}
            {nextOpen && (!finding || finding.findingStatus !== "open") && (
              <button type="button" className="desk-next" onClick={() => select(nextOpen.position)}>
                {t("workspace.nextOpen")}
              </button>
            )}
          </section>
          <section className="desk-overview" aria-labelledby="review-summary-title">
            <h2 id="review-summary-title">{t("summary.title")}</h2>
            <div
              className="desk-summary-body"
              tabIndex={0}
              role="region"
              aria-label={t("summary.title")}
            >
              <p className="text-sm leading-relaxed">
                {summary?.recommendation ?? t("home.status." + record.status)}
              </p>
              <ul data-testid="risk-summary">
                {SUMMARY.map(({ key, count, style }) => (
                  <li key={key} style={{ color: style.color }}>
                    <style.icon aria-hidden size={17} />
                    {t("workspace.counts." + key, { count: record.counts?.[count] ?? 0 })}
                  </li>
                ))}
              </ul>

              <DecisionProgress review={record} />
              {summary?.top_risks?.length ? (
                <div>
                  <h3>{t("summary.topRisks")}</h3>
                  {summary.top_risks.map((r) => (
                    <button
                      type="button"
                      key={r.position}
                      className="desk-risk-link"
                      onClick={() => select(r.position)}
                    >
                      {review.byPosition.get(r.position)?.clause.heading ?? r.number}{" "}
                      <span className="block line-clamp-2 text-sm text-muted-foreground">
                        {r.excerpt}
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
              {summary?.void_groups?.length ? (
                <div>
                  <h3>{t("summary.voidGroups")}</h3>
                  {summary.void_groups.map((group) => (
                    <p key={group.key} className="text-sm">
                      {group.label} ({group.findings.length})
                    </p>
                  ))}
                </div>
              ) : null}
              <footer className="desk-tools">
                <ReviewerControl />
                <WorkspaceHotkeys />
                <details>
                  <summary>{t("home.table.usage")}</summary>
                  <ReviewCost
                    review={record}
                    pricing={review.pricing}
                    costEur={review.costEur}
                    wallTimeMs={review.wallTimeMs}
                    cacheHitRate={review.cacheHitRate}
                    compact
                  />
                </details>
              </footer>
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}

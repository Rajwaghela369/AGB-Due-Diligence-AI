import { AlertTriangle, Download, FileWarning } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ClauseVerdict, ReviewSummary } from "../../api/types.gen";
import { riskStyle } from "../review/risk";
import { ExportDialog } from "./export-dialog";

export function SummaryBlock({ reviewId, summary }: { reviewId: string; summary: ReviewSummary }) {
  const { t } = useTranslation();
  const counts = summary.counts;
  return (
    <section
      aria-labelledby="review-summary-title"
      data-testid="review-summary"
      className="rounded-xl border border-border bg-surface p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="review-summary-title" className="text-lg font-semibold">
            {t("summary.title")}
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">{summary.recommendation}</p>
        </div>
        <ExportDialog reviewId={reviewId} defaultDraft={summary.draft}>
          <Download aria-hidden className="size-4" />
          {t("export.action")}
        </ExportDialog>
      </div>
      <dl className="mt-4 grid gap-2 sm:grid-cols-3">
        {[
          ["potentially_void", counts.potentially_void ?? 0],
          ["uncertain", counts.uncertain ?? 0],
          ["valid", counts.valid ?? 0],
        ].map(([verdict, count]) => {
          const style = riskStyle({
            status: "done",
            verdict: { verdict: verdict as ClauseVerdict["verdict"] } as ClauseVerdict,
          });
          const Icon = style.icon;
          return (
            <div
              key={verdict}
              className="flex items-center gap-2 rounded-md bg-muted px-3 py-2 text-sm"
            >
              <Icon aria-hidden className="size-4" style={{ color: style.color }} />
              <dt>{t(style.label)}</dt>
              <dd className="ml-auto tabular-nums font-medium">{count}</dd>
            </div>
          );
        })}
      </dl>
      {(summary.top_risks?.length ?? 0) > 0 && (
        <div className="mt-4">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangle aria-hidden className="size-4 text-[var(--risk)]" />
            {t("summary.topRisks")}
          </h3>
          <ol className="mt-2 space-y-2">
            {summary.top_risks?.map((finding) => (
              <li
                key={finding.position}
                className="rounded-md border border-border px-3 py-2 text-sm"
              >
                <p className="font-medium">{finding.number ?? t("summary.unnumbered")}</p>
                <p className="mt-0.5 text-muted-foreground">{finding.excerpt}</p>
              </li>
            ))}
          </ol>
        </div>
      )}
      {(summary.void_groups?.length ?? 0) > 0 && (
        <div className="mt-4">
          <h3 className="flex items-center gap-2 text-sm font-medium">
            <FileWarning aria-hidden className="size-4" />
            {t("summary.voidGroups")}
          </h3>
          <ul className="mt-2 flex flex-wrap gap-2 text-sm">
            {summary.void_groups?.map((group) => (
              <li key={group.key} className="rounded-full border border-border px-2.5 py-1">
                {group.label} <span className="tabular-nums">({group.findings.length})</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

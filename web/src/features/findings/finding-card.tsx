// One finding in the side panel. Hovering sets `hover` and clicking sets `select` in the
// shared review UI state, so the document and the table mirror what the pointer or keyboard
// is on. The live states stay quiet: no colour, just an icon and a label.
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import { confidenceBand, riskStyle } from "../review/risk";
import { CardActions } from "../decisions/card-actions";
import { StatusBadge } from "../decisions/status-badge";
import { isDecidable } from "../decisions/status";
import { useReviewUi } from "../review/store";
import type { ReviewFinding } from "../review/use-review";
import { FeedbackControl } from "./feedback-control";
import { clauseTitle } from "./helpers";
import { strongestBasis, useStatute } from "./use-statute";

export function FindingCard(props: { finding: ReviewFinding; reviewId: string }) {
  const { finding, reviewId } = props;
  const { t } = useTranslation();
  const style = riskStyle(finding);
  const Icon = style.icon;
  const selected = useReviewUi((s) => s.selected === finding.position);
  const hovered = useReviewUi((s) => s.hovered === finding.position);
  const select = useReviewUi((s) => s.select);
  const hover = useReviewUi((s) => s.hover);

  const verdict = finding.verdict;
  const chunkId = strongestBasis(verdict?.legal_basis, verdict?.basis_scores);
  const statute = useStatute(chunkId);
  const live = style.tone === "queued" || style.tone === "checking" || style.tone === "failed";
  const decision = finding.decision;
  // The reasoning written for the lawyer's verdict replaces the model's (docs/Phase 7 API.md).
  const reason =
    decision?.reason_unavailable || (!decision?.reason_de && verdict?.reason_unavailable)
      ? t("drawer.reasonUnavailable")
      : decision?.reason_de || verdict?.reason_de;

  return (
    <article
      data-testid={`finding-card-${finding.position}`}
      data-selected={selected}
      aria-current={selected ? "true" : undefined}
      onMouseEnter={() => hover(finding.position)}
      onMouseLeave={() => hover(null)}
      className={cn(
        "rounded-lg border border-border bg-surface",
        (selected || hovered) && "border-l-4",
        selected && "ring-2 ring-ring",
        hovered && !selected && "bg-muted",
        live && "border-dashed",
      )}
      style={selected || hovered ? { borderLeftColor: style.color } : undefined}
    >
      <button
        type="button"
        onClick={() => select(finding.position)}
        onFocus={() => hover(finding.position)}
        onBlur={() => hover(null)}
        className="block w-full rounded-lg p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className="flex items-center gap-2 text-sm font-medium">
          <Icon
            className={cn("size-4 shrink-0", style.tone === "checking" && "animate-spin")}
            style={{ color: style.color }}
            aria-hidden
          />
          <span style={{ color: style.color }}>{t(style.label)}</span>
          {verdict && !live && (
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              {t(`findings.confidence.${confidenceBand(verdict.confidence)}`)}
            </span>
          )}
        </span>
        <span className="mt-1 block text-sm">{clauseTitle(finding)}</span>
        {isDecidable(finding) && (
          <span className="mt-1.5 block">
            <StatusBadge finding={finding} />
          </span>
        )}
        {chunkId && (
          <span className="mt-0.5 block text-xs text-muted-foreground">
            {statute.data?.citation ?? chunkId}
          </span>
        )}
        {reason && (
          <span className="mt-1 line-clamp-1 block text-xs text-muted-foreground">{reason}</span>
        )}
        {style.tone === "failed" && (
          <span className="mt-1 block text-xs text-muted-foreground">
            {t("findings.failedHint")}
          </span>
        )}
      </button>
      {verdict && !live && (
        <div className="flex flex-wrap items-center justify-between gap-1 border-t border-border px-3 py-1.5">
          <FeedbackControl reviewId={reviewId} finding={finding} compact />
          {isDecidable(finding) && (
            <CardActions
              reviewId={reviewId}
              position={finding.position}
              current={decision?.action}
            />
          )}
        </div>
      )}
    </article>
  );
}

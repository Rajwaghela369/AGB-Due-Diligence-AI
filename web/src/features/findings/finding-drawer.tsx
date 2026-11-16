// The detail drawer for the selected clause (Phase 6, 6.D and 6.F). It is a non-modal
// Radix Dialog on purpose: the findings panel and the document stay clickable while it is
// open, so selecting another clause just swaps the drawer's content. Esc closes it.
import { ChevronDown, ChevronRight, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../../components/ui/dialog";
import { confidenceBand, riskStyle } from "../review/risk";
import { useReviewUi } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import { FeedbackControl } from "./feedback-control";
import { clauseTitle } from "./helpers";
import { TechnicalDetails } from "./technical-details";
import { useStatute } from "./use-statute";

function StatuteItem({ chunkId, defaultOpen }: { chunkId: string; defaultOpen: boolean }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(defaultOpen);
  const statute = useStatute(chunkId, open);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <li className="rounded-md border border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 p-2 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Chevron className="size-4 shrink-0" aria-hidden />
        {statute.data?.citation ?? chunkId}
      </button>
      {open && (
        <div className="border-t border-border p-3 text-sm">
          {statute.isLoading && (
            <p className="text-muted-foreground">{t("drawer.statuteLoading")}</p>
          )}
          {statute.isError && <p role="alert">{t("drawer.statuteError")}</p>}
          {statute.data && (
            <>
              <p className="font-contract whitespace-pre-line leading-relaxed">
                {statute.data.text}
              </p>
              {statute.data.source_url && (
                <a
                  href={statute.data.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-2 inline-block text-xs underline"
                >
                  {t("drawer.statuteSource")}
                </a>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

function DrawerBody({ finding, reviewId }: { finding: ReviewFinding; reviewId: string }) {
  const { t } = useTranslation();
  const style = riskStyle(finding);
  const Icon = style.icon;
  const verdict = finding.verdict;

  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Icon
          className={style.tone === "checking" ? "size-4 animate-spin" : "size-4"}
          style={{ color: style.color }}
          aria-hidden
        />
        <span style={{ color: style.color }}>{t(style.label)}</span>
        {verdict && (
          <span className="ml-auto font-normal text-muted-foreground">
            {t("drawer.confidence")}:{" "}
            {t(`findings.confidence.${confidenceBand(verdict.confidence)}`)}
          </span>
        )}
      </div>

      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {t("drawer.clause")}
        </h3>
        <p
          className="font-contract whitespace-pre-line leading-relaxed"
          data-testid="drawer-clause"
        >
          {finding.clause.text}
        </p>
      </section>

      {verdict && (
        <>
          <section>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("drawer.legalBasis")}
            </h3>
            {verdict.legal_basis && verdict.legal_basis.length > 0 ? (
              <ul className="space-y-2">
                {verdict.legal_basis.map((chunkId, index) => (
                  <StatuteItem key={chunkId} chunkId={chunkId} defaultOpen={index === 0} />
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">{t("drawer.noBasis")}</p>
            )}
          </section>

          <section>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("drawer.reasoning")}
            </h3>
            <p className="text-sm leading-relaxed">
              {verdict.reason_unavailable ? t("drawer.reasonUnavailable") : verdict.reason_de}
            </p>
          </section>

          <FeedbackControl reviewId={reviewId} finding={finding} />
        </>
      )}

      {!verdict && (
        <p className="text-sm text-muted-foreground">
          {finding.status === "failed" ? t("findings.failedHint") : t("drawer.noVerdict")}
        </p>
      )}

      <TechnicalDetails finding={finding} reviewId={reviewId} />
    </div>
  );
}

/** Detail drawer for the selected clause: verdict, statute text, reasoning, feedback (6.D, 6.F). */
export function FindingDrawer({ review }: { review: ReviewData }) {
  const { t } = useTranslation();
  const selected = useReviewUi((s) => s.selected);
  const select = useReviewUi((s) => s.select);
  const finding = selected === null ? undefined : review.byPosition.get(selected);
  const reviewId = review.review?.review_id;

  return (
    <Dialog
      modal={false}
      open={finding !== undefined && reviewId !== undefined}
      onOpenChange={(open) => {
        if (!open) select(null);
      }}
    >
      {finding && reviewId && (
        <DialogContent
          aria-label={clauseTitle(finding)}
          data-testid="finding-drawer"
          // Clicks on cards or the document must change the selection, not dismiss the drawer.
          onInteractOutside={(event) => event.preventDefault()}
          onOpenAutoFocus={(event) => event.preventDefault()}
        >
          <div className="mb-4 flex items-start justify-between gap-3">
            <DialogTitle className="text-base font-semibold">{clauseTitle(finding)}</DialogTitle>
            <DialogClose
              aria-label={t("drawer.close")}
              className="rounded-md p-1 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <X className="size-4" aria-hidden />
            </DialogClose>
          </div>
          <DialogDescription className="sr-only">{t("drawer.description")}</DialogDescription>
          <DrawerBody key={finding.position} finding={finding} reviewId={reviewId} />
        </DialogContent>
      )}
    </Dialog>
  );
}

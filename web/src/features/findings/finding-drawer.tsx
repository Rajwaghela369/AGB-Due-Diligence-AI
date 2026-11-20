// The selected finding stays alongside the PDF and queue on desktop.
import { ArrowLeft, ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { RewriteChat } from "../chat/rewrite-chat";
import { AuditList } from "../decisions/audit-list";
import { DecisionPanel } from "../decisions/decision-panel";
import { confidenceBand, riskStyle } from "../review/risk";
import { useReviewUi } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import type { Pricing } from "../../api/types.gen";
import { FeedbackControl } from "./feedback-control";
import { RegenerateReason } from "./regenerate-reason";
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

function DrawerBody({
  finding,
  reviewId,
  pricing,
}: {
  finding: ReviewFinding;
  reviewId: string;
  pricing: Pricing | undefined;
}) {
  const { t } = useTranslation();
  const style = riskStyle(finding);
  const Icon = style.icon;
  const verdict = finding.verdict;
  const decision = finding.decision;

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
            {t(`drawer.confidenceBands.${confidenceBand(verdict.confidence)}`)}
          </span>
        )}
      </div>

      {verdict && (
        <>
          <section>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("drawer.reasoning")}
            </h3>
            {decision?.reason_de ? (
              <p className="text-sm leading-relaxed">{decision.reason_de}</p>
            ) : decision?.reason_unavailable ? (
              <p className="text-sm leading-relaxed">{t("drawer.reasonUnavailable")}</p>
            ) : verdict.reason_unavailable && verdict.verdict === "potentially_void" ? (
              <RegenerateReason reviewId={reviewId} position={finding.position} />
            ) : verdict.reason_unavailable ? (
              <p className="text-sm leading-relaxed">{t("drawer.reasonUnavailable")}</p>
            ) : (
              <p className="text-sm leading-relaxed">
                {verdict.reason_de?.trim() || t("drawer.reasonUnavailable")}
              </p>
            )}
          </section>

          <DecisionPanel reviewId={reviewId} finding={finding} />
        </>
      )}
      <details>
        <summary className="cursor-pointer text-sm font-medium">{t("drawer.clause")}</summary>
        <p
          className="font-contract whitespace-pre-line leading-relaxed"
          data-testid="drawer-clause"
        >
          {finding.clause.text}
        </p>
      </details>

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

          {(finding.status === "done" || finding.status === "awaiting_decision") && (
            <RewriteChat reviewId={reviewId} finding={finding} />
          )}

          <AuditList reviewId={reviewId} position={finding.position} />

          <FeedbackControl reviewId={reviewId} finding={finding} />
        </>
      )}

      {!verdict && (
        <p className="text-sm text-muted-foreground">
          {finding.status === "failed" ? t("findings.failedHint") : t("drawer.noVerdict")}
        </p>
      )}

      <TechnicalDetails finding={finding} reviewId={reviewId} pricing={pricing} />
    </div>
  );
}

/** Detail view for the selected clause: verdict, statute text, reasoning, feedback (6.D, 6.F). */
export function FindingDrawer({ review, onBack }: { review: ReviewData; onBack?: () => void }) {
  const { t } = useTranslation();
  const selected = useReviewUi((s) => s.selected);
  const select = useReviewUi((s) => s.select);
  const finding = selected === null ? undefined : review.byPosition.get(selected);
  const reviewId = review.review?.review_id;
  const open = finding !== undefined && reviewId !== undefined;

  // The card that opened the detail, so Back or Esc can hand focus back to it.
  const trigger = useRef<HTMLElement | null>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest('[data-testid="finding-drawer"]')) return;
      trigger.current = target.closest('[data-testid^="finding-card-"]') ? target : null;
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, []);
  useEffect(() => {
    const panel = backRef.current?.closest("section");
    if (panel) panel.scrollTop = 0;
  }, [selected]);
  useEffect(() => {
    if (!open) return;
    backRef.current?.focus({ preventScroll: true });
    return () => {
      const card = trigger.current;
      if (card?.isConnected) card.focus({ preventScroll: true });
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      // Leave Esc to the shortcut sheet when it is open.
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (document.querySelector('[role="dialog"]')) return;
      select(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, select]);

  if (!finding || !reviewId) return null;
  return (
    <section
      aria-label={clauseTitle(finding)}
      data-testid="finding-drawer"
      className="flex min-h-0 flex-1 flex-col overflow-y-auto rounded-xl border border-border bg-surface p-5 shadow-sm"
    >
      <button
        ref={backRef}
        type="button"
        onClick={() => {
          select(null);
          onBack?.();
        }}
        className="mb-3 inline-flex w-fit items-center gap-1 rounded-md px-1.5 py-1 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft className="size-4" aria-hidden />
        {t("findings.title")}
      </button>
      <h2 className="mb-5 text-xl font-semibold leading-tight tracking-tight">
        {clauseTitle(finding)}
      </h2>
      <p className="sr-only">{t("drawer.description")}</p>
      <DrawerBody
        key={finding.position}
        finding={finding}
        reviewId={reviewId}
        pricing={review.pricing}
      />
    </section>
  );
}

// Collapsed "Technical details": what a developer or a curious lawyer checks when a verdict
// looks off. Per-step votes live here only, behind the `agb.dev` localStorage switch, so
// the everyday drawer keeps one simple 👍 / 👎.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, ExternalLink, RotateCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import type { FeedbackIn } from "../../api/types.gen";
import { reviewKeys, type ReviewFinding } from "../review/use-review";
import { isDevMode, langfuseTraceUrl } from "./helpers";
import { useFeedback } from "./use-feedback";

type Reason = NonNullable<FeedbackIn["reasons"]>[number];

/** Which feedback reason scores which pipeline step (the server derives `scored_steps`). */
const STEPS: { step: "clause_span" | "retrieval" | "judge" | "explanation"; reason: Reason }[] = [
  { step: "clause_span", reason: "clause_cut" },
  { step: "retrieval", reason: "basis_wrong" },
  { step: "judge", reason: "verdict_wrong" },
  { step: "explanation", reason: "reasoning_unclear" },
];

export function TechnicalDetails({
  finding,
  reviewId,
}: {
  finding: ReviewFinding;
  reviewId: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const stepVote = useFeedback(reviewId);
  const retry = useMutation({
    mutationFn: () => api.reviews.retryClause(reviewId, finding.position),
    onSettled: () => queryClient.invalidateQueries({ queryKey: reviewKeys.findings(reviewId) }),
  });
  const verdict = finding.verdict;
  const traceUrl = langfuseTraceUrl(verdict?.trace_id);
  const dev = isDevMode();
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <section className="rounded-md border border-border">
      <h3 className="text-sm font-medium">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Chevron className="size-4" aria-hidden />
          {t("drawer.technical")}
        </button>
      </h3>
      {open && (
        <div className="space-y-4 border-t border-border p-3 text-xs">
          {finding.status === "failed" && (
            <div>
              {finding.error && <p className="mb-2 text-muted-foreground">{finding.error}</p>}
              <button
                type="button"
                disabled={retry.isPending}
                onClick={() => retry.mutate()}
                className="inline-flex items-center gap-1 rounded-md border border-border px-3 py-1.5 hover:bg-muted disabled:opacity-50"
              >
                <RotateCw className="size-3.5" aria-hidden />
                {t("drawer.retry")}
              </button>
              {retry.isError && (
                <p role="alert" className="mt-1">
                  {t("drawer.retryError")}
                </p>
              )}
            </div>
          )}

          {verdict && (
            <>
              <p>
                {t("drawer.confidenceNumeric")}: {verdict.confidence.toFixed(2)}
              </p>
              <div>
                <h4 className="mb-1 font-medium">{t("drawer.candidates")}</h4>
                {verdict.candidates && verdict.candidates.length > 0 ? (
                  <ul className="space-y-0.5">
                    {verdict.candidates.map((id) => (
                      <li key={id} className="flex justify-between gap-2">
                        <span>{id}</span>
                        <span className="text-muted-foreground">
                          {verdict.basis_scores?.[id]?.toFixed(2) ?? "–"}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-muted-foreground">{t("drawer.noCandidates")}</p>
                )}
              </div>
              {traceUrl && (
                <a
                  href={traceUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 underline"
                >
                  {t("drawer.trace")}
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              )}
            </>
          )}

          {dev && verdict && (
            <div data-testid="step-votes">
              <h4 className="mb-1 font-medium">{t("feedback.steps")}</h4>
              <ul className="space-y-1">
                {STEPS.filter(({ step }) => verdict.observations?.[step]).map(
                  ({ step, reason }) => (
                    <li key={step} className="flex items-center justify-between gap-2">
                      <span>{t(`feedback.step.${step}`)}</span>
                      <button
                        type="button"
                        aria-label={t("feedback.stepDown", { step: t(`feedback.step.${step}`) })}
                        onClick={() =>
                          stepVote.mutate({
                            review_id: reviewId,
                            position: finding.position,
                            vote: "down",
                            reasons: [reason],
                          })
                        }
                        className="rounded-md border border-border px-2 py-1 hover:bg-muted"
                      >
                        👎
                      </button>
                    </li>
                  ),
                )}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// How far the lawyer is: one bar for decided / decidable findings, with the open void and
// review findings counted separately, and „Prüfung abschließen“ once none of them is open.
// For now closing confirms and returns home; Phase 8 adds the export.
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { CheckCheck } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import type { Review } from "../../api/types.gen";
import { progressOf } from "./progress";

export function ProgressBar({ review }: { review: Pick<Review, "counts" | "status"> }) {
  const { t } = useTranslation();
  const progress = progressOf(review);
  const percent = Math.round(progress.fraction * 100);
  return (
    <div className="min-w-0 flex-1 space-y-1">
      <div
        role="progressbar"
        aria-label={t("progress.label")}
        aria-valuemin={0}
        aria-valuemax={progress.decidable}
        aria-valuenow={progress.decided}
        aria-valuetext={t("progress.text", {
          decided: progress.decided,
          total: progress.decidable,
        })}
        className="h-2 overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full bg-accent" style={{ width: `${percent}%` }} />
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">
        {t("progress.text", { decided: progress.decided, total: progress.decidable })}
      </p>
    </div>
  );
}

export function DecisionProgress({ review }: { review: Review }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [confirming, setConfirming] = useState(false);
  const progress = progressOf(review);
  return (
    <div
      data-testid="decision-progress"
      className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-surface px-3 py-2"
    >
      <ProgressBar review={review} />
      <ul className="flex flex-wrap gap-x-3 text-xs" aria-label={t("progress.open")}>
        <li data-testid="open-void">{t("progress.openVoid", { count: progress.openVoid })}</li>
        <li data-testid="open-review">
          {t("progress.openReview", { count: progress.openReview })}
        </li>
        <li data-testid="decided">{t("progress.decided", { count: progress.decided })}</li>
      </ul>
      <button
        type="button"
        disabled={!progress.canComplete}
        onClick={() => setConfirming(true)}
        title={progress.canComplete ? undefined : t("progress.completeDisabled")}
        className="inline-flex h-8 items-center gap-1.5 rounded-md bg-foreground px-3 text-sm text-background hover:opacity-85 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CheckCheck aria-hidden="true" size={14} />
        {t("progress.complete")}
      </button>
      <DialogPrimitive.Root open={confirming} onOpenChange={setConfirming}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/30" />
          <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 w-[min(26rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-surface p-5 text-foreground shadow-lg focus-visible:outline-none">
            <DialogPrimitive.Title className="text-base font-semibold">
              {t("progress.confirmTitle")}
            </DialogPrimitive.Title>
            <DialogPrimitive.Description className="mt-1 text-sm text-muted-foreground">
              {t("progress.confirmText")}
            </DialogPrimitive.Description>
            <div className="mt-5 flex justify-end gap-2">
              <DialogPrimitive.Close className="rounded-md px-3 py-1.5 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {t("progress.cancel")}
              </DialogPrimitive.Close>
              <button
                type="button"
                onClick={() => {
                  setConfirming(false);
                  navigate("/");
                }}
                className="rounded-md bg-foreground px-3 py-1.5 text-sm text-background hover:opacity-85 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t("progress.confirm")}
              </button>
            </div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </div>
  );
}

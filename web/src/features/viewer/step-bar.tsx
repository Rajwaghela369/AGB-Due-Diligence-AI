// Upload ✓ → Lesen ✓ → Aufteilen ✓ → Prüfung n/N while a review runs. The first three are
// done by the time a review exists (its clauses come from the split), so only the check
// step moves, driven by the live review counts.
import { Check, Hourglass, Loader2 } from "lucide-react";
import { Fragment } from "react";
import { useTranslation } from "react-i18next";

import type { ReviewProgress } from "../../api/events";
import type { Review } from "../../api/types.gen";
import { reviewProgress } from "./layout";

export function StepBar(props: { review: Review; progress?: ReviewProgress | null }) {
  const { t } = useTranslation();
  const { done, total } = reviewProgress(props.review);
  const awaitingDecisions = props.review.status === "awaiting_decisions";
  const finished = ["upload", "read", "split"] as const;
  return (
    <ol
      aria-label={t("viewer.steps.label")}
      className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-3 py-2 text-xs"
    >
      {finished.map((step) => (
        <Fragment key={step}>
          <li className="flex items-center gap-1 text-muted-foreground">
            <Check aria-hidden="true" size={13} className="text-valid" />
            {t(`viewer.steps.${step}`)}
            <span className="sr-only">{t("viewer.steps.done")}</span>
          </li>
          <li aria-hidden="true" className="text-muted-foreground">
            →
          </li>
        </Fragment>
      ))}
      <li className="flex items-center gap-2 font-medium" aria-current="step">
        {awaitingDecisions ? (
          <Check aria-hidden="true" size={13} className="text-valid" />
        ) : (
          <Loader2
            aria-hidden="true"
            size={13}
            className="animate-spin motion-reduce:animate-none"
          />
        )}
        {t("viewer.steps.check", { done, total })}
        <progress
          className="h-1.5 w-24 overflow-hidden rounded-full accent-[var(--accent)]"
          max={Math.max(total, 1)}
          value={done}
          aria-label={t("viewer.steps.check", { done, total })}
        />
      </li>
      {awaitingDecisions && (
        <li role="status" className="text-muted-foreground">
          {t("viewer.steps.awaitingDecisions")}
        </li>
      )}
      {props.progress?.waiting_reason && (
        <li role="status" className="flex items-center gap-1 text-muted-foreground">
          <Hourglass aria-hidden="true" size={13} />
          {t(`viewer.steps.waiting.${props.progress.waiting_reason}`)}
        </li>
      )}
    </ol>
  );
}

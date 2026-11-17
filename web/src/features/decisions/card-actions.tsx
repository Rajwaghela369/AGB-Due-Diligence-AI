// Accept / override / dismiss as small icon buttons on a finding card. Accept and dismiss are
// sent at once; override opens the drawer's picker, because choosing a verdict and a § needs
// the room. They file the same request a hotkey does.
import { Ban, CircleCheck, PencilLine } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import { useReviewUi, type DecisionAction } from "../review/store";
import { useDecide } from "./use-decide";

const button = cn(
  "inline-flex items-center rounded-md border border-transparent p-1.5",
  "hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  "disabled:opacity-60",
);

export function CardActions(props: {
  reviewId: string;
  position: number;
  current: DecisionAction | undefined;
}) {
  const { t } = useTranslation();
  const { decide, pending, error } = useDecide(props.reviewId);
  const select = useReviewUi((s) => s.select);
  const requestDecision = useReviewUi((s) => s.requestDecision);

  return (
    <div className="flex items-center gap-0.5" data-testid={`card-actions-${props.position}`}>
      <button
        type="button"
        disabled={pending}
        aria-pressed={props.current === "accept"}
        aria-label={t("decision.accept")}
        title={`${t("decision.accept")} (A)`}
        onClick={() => void decide(props.position, { action: "accept", note: "" })}
        className={cn(button, props.current === "accept" && "border-border bg-muted")}
      >
        <CircleCheck className="size-3.5" aria-hidden />
      </button>
      <button
        type="button"
        disabled={pending}
        aria-pressed={props.current === "override"}
        aria-label={t("decision.override.open")}
        title={`${t("decision.override.open")} (O)`}
        onClick={() => {
          select(props.position);
          requestDecision("override", props.position);
        }}
        className={cn(button, props.current === "override" && "border-border bg-muted")}
      >
        <PencilLine className="size-3.5" aria-hidden />
      </button>
      <button
        type="button"
        disabled={pending}
        aria-pressed={props.current === "dismiss"}
        aria-label={t("decision.dismiss")}
        title={`${t("decision.dismiss")} (D)`}
        onClick={() => void decide(props.position, { action: "dismiss", note: "" })}
        className={cn(button, props.current === "dismiss" && "border-border bg-muted")}
      >
        <Ban className="size-3.5" aria-hidden />
      </button>
      {error && (
        <span role="alert" className="ml-1 text-xs text-risk">
          {error}
        </span>
      )}
    </div>
  );
}

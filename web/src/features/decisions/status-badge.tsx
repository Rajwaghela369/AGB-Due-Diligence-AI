// The finding's decision state as icon + label. `awaiting_decision` clauses read
// „Entscheidung nötig“ whatever their finding status is.
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import type { ReviewFinding } from "../review/use-review";
import { decisionStyle } from "./status";

export function StatusBadge(props: {
  finding: Pick<ReviewFinding, "status" | "findingStatus">;
  className?: string;
}) {
  const { t } = useTranslation();
  const style = decisionStyle(props.finding);
  const Icon = style.icon;
  return (
    <span
      data-testid="decision-status"
      data-status={style.tone}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs",
        props.className,
      )}
      style={{ color: style.color }}
    >
      <Icon aria-hidden className="size-3.5 shrink-0" />
      {t(style.label)}
    </span>
  );
}

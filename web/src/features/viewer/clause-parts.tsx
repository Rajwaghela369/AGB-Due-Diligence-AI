// Pieces shared by the PDF and text views, so a clause is marked, labelled and retried the
// same way in both.
import { RotateCcw } from "lucide-react";
import type { CSSProperties } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import type { ReviewFinding } from "../review/use-review";
import type { HighlightStyle } from "./highlight";
import { clauseName, type ClauseInteraction } from "./layout";

/**
 * The clause's keyboard and screen-reader handle: the state icon plus an accessible label.
 * Out-of-scope clauses have none.
 */
export function ClauseMarker(props: {
  finding: ReviewFinding;
  style: HighlightStyle;
  interaction: ClauseInteraction;
  className?: string;
  css?: CSSProperties;
}) {
  const { finding, style, interaction } = props;
  const { t } = useTranslation();
  const Icon = style.icon;
  const label = t("viewer.clause", { name: clauseName(finding), state: t(style.label) });
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={interaction.selected === finding.position}
      title={label}
      data-tone={style.tone}
      className={cn(
        "flex items-center justify-center rounded-sm bg-surface/70 focus-visible:outline-2 " +
          "focus-visible:outline-offset-1 focus-visible:outline-ring",
        style.tone === "valid" && "opacity-60",
        props.className,
      )}
      style={{ color: style.color, ...props.css }}
      onClick={() => interaction.select(finding.position)}
      onFocus={() => interaction.hover(finding.position)}
      onBlur={() => interaction.hover(null)}
      onMouseEnter={() => interaction.hover(finding.position)}
      onMouseLeave={() => interaction.hover(null)}
    >
      <Icon
        aria-hidden="true"
        size={14}
        className={cn(style.tone === "checking" && "animate-spin motion-reduce:animate-none")}
      />
    </button>
  );
}

/** „Konnte nicht geprüft werden · Erneut versuchen" for a failed clause. */
export function RetryChip(props: {
  finding: ReviewFinding;
  interaction: ClauseInteraction;
  className?: string;
  css?: CSSProperties;
}) {
  const { finding, interaction } = props;
  const { t } = useTranslation();
  const busy = interaction.retrying.has(finding.position);
  return (
    <span
      role="status"
      className={cn(
        "inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2 py-0.5 " +
          "font-sans text-xs text-foreground shadow-sm",
        props.className,
      )}
      style={props.css}
    >
      <span>{t("risk.failed")}</span>
      <span aria-hidden="true">·</span>
      <button
        type="button"
        disabled={busy}
        className={
          "inline-flex items-center gap-1 rounded-sm font-medium text-accent underline-offset-2 " +
          "hover:underline focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-60"
        }
        aria-label={t("viewer.retryClause", { name: clauseName(finding) })}
        onClick={(event) => {
          event.stopPropagation();
          interaction.retry(finding.position);
        }}
      >
        <RotateCcw aria-hidden="true" size={12} />
        {busy ? t("viewer.retrying") : t("viewer.retry")}
      </button>
    </span>
  );
}

// How each clause state is drawn on the document, in one table so PDF and text mode look
// the same. Colours come only from `riskStyle`; this decides how loudly to use them
// (Phase 6 principle 3: red and amber stand out, valid is a faint mark, out of scope is
// not drawn at all).
import type { LucideIcon } from "lucide-react";

import { riskStyle, type Tone } from "../review/risk";
import type { ReviewFinding } from "../review/use-review";

export type BarStyle = "solid" | "dashed" | "faint" | "none";

export interface HighlightStyle {
  tone: Tone;
  icon: LucideIcon;
  /** i18n key under `risk.` */
  label: string;
  /** Margin bar colour (a CSS value from riskStyle). */
  color: string;
  bar: BarStyle;
  /** Background tint for the clause's lines, or null for none. */
  tint: string | null;
  /** Soft pulse while the clause is being checked. */
  pulse: boolean;
  /** Shows the "could not be checked · retry" control. */
  retry: boolean;
  /** Drawn at all; out-of-scope clauses only keep an invisible scroll anchor. */
  visible: boolean;
}

const BARS: Record<Tone, BarStyle> = {
  void: "solid",
  review: "solid",
  valid: "faint",
  queued: "dashed",
  checking: "solid",
  failed: "solid",
  out: "none",
};

/** Tint strength per tone, as a percentage of the tone colour over transparent. */
const TINTS: Partial<Record<Tone, number>> = { void: 14, review: 16, checking: 8 };

export function tint(color: string, percent: number): string {
  return `color-mix(in srgb, ${color} ${percent}%, transparent)`;
}

export function highlightStyle(finding: Pick<ReviewFinding, "status" | "verdict">): HighlightStyle {
  const risk = riskStyle(finding);
  const strength = TINTS[risk.tone];
  return {
    tone: risk.tone,
    icon: risk.icon,
    label: risk.label,
    color: risk.color,
    bar: BARS[risk.tone],
    tint: strength === undefined ? null : tint(risk.color, strength),
    pulse: risk.tone === "checking",
    retry: risk.tone === "failed",
    visible: risk.tone !== "out",
  };
}

/** The extra emphasis when another panel hovers or selects the clause. */
export function emphasisTint(style: HighlightStyle, emphasised: boolean): string | null {
  if (!emphasised || !style.visible) return style.tint;
  const color = style.tone === "valid" || style.tone === "queued" ? "var(--accent)" : style.color;
  return tint(color, 26);
}

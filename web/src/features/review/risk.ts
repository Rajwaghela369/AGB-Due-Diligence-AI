// One mapping from a clause's state to how it looks, shared by every view so a void
// clause is the same red with the same icon and label everywhere. Colour is never the
// only signal: each tone has an icon and a label key (Phase 6 principle 3).
import {
  AlertTriangle,
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleSlash,
  Loader2,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { ReviewFinding } from "./use-review";

export type Tone = "void" | "review" | "valid" | "queued" | "checking" | "failed" | "out";

export interface RiskStyle {
  tone: Tone;
  icon: LucideIcon;
  /** i18n key under `risk.` */
  label: string;
  /** CSS custom property from styles.css (light and dark); pair it with the `-soft` tint. */
  color: string;
}

const STYLES: Record<Tone, RiskStyle> = {
  void: { tone: "void", icon: AlertTriangle, label: "risk.void", color: "var(--risk)" },
  review: { tone: "review", icon: CircleHelp, label: "risk.review", color: "var(--review)" },
  valid: { tone: "valid", icon: CircleCheck, label: "risk.valid", color: "var(--valid)" },
  queued: {
    tone: "queued",
    icon: CircleDashed,
    label: "risk.queued",
    color: "var(--muted-foreground)",
  },
  checking: {
    tone: "checking",
    icon: Loader2,
    label: "risk.checking",
    color: "var(--muted-foreground)",
  },
  failed: { tone: "failed", icon: XCircle, label: "risk.failed", color: "var(--muted-foreground)" },
  out: { tone: "out", icon: CircleSlash, label: "risk.out", color: "transparent" },
};

export function toneOf(finding: Pick<ReviewFinding, "status" | "verdict">): Tone {
  if (finding.status === "out_of_scope") return "out";
  if (finding.status === "failed") return "failed";
  if (finding.status === "queued") return "queued";
  if (finding.status === "checking") return "checking";
  if (finding.status === "awaiting_decision") return "review";
  switch (finding.verdict?.verdict) {
    case "potentially_void":
      return "void";
    case "uncertain":
      return "review";
    default:
      return "valid";
  }
}

export function riskStyle(finding: Pick<ReviewFinding, "status" | "verdict">): RiskStyle {
  return STYLES[toneOf(finding)];
}

/** Confidence as the lawyer sees it; the number lives in "Technical details". */
export function confidenceBand(confidence: number): "high" | "medium" | "low" {
  if (confidence >= 0.8) return "high";
  if (confidence >= 0.6) return "medium";
  return "low";
}

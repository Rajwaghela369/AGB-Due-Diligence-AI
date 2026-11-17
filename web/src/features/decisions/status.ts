// One mapping from a finding's decision state to icon, label and colour, shared by cards, the
// table and the drawer. Like `risk.ts`, colour is never the only signal.
import { Ban, CircleCheck, CircleDashed, PencilLine, UserRoundCheck } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { FindingStatus } from "../../api/types.gen";
import type { ReviewFinding } from "../review/use-review";

export type DecisionTone = FindingStatus | "awaiting";

export interface DecisionStyle {
  tone: DecisionTone;
  icon: LucideIcon;
  /** i18n key under `decision.status.` */
  label: string;
  color: string;
}

const STYLES: Record<DecisionTone, DecisionStyle> = {
  open: {
    tone: "open",
    icon: CircleDashed,
    label: "decision.status.open",
    color: "var(--muted-foreground)",
  },
  accepted: {
    tone: "accepted",
    icon: CircleCheck,
    label: "decision.status.accepted",
    color: "var(--valid)",
  },
  overridden: {
    tone: "overridden",
    icon: PencilLine,
    label: "decision.status.overridden",
    color: "var(--accent)",
  },
  dismissed: {
    tone: "dismissed",
    icon: Ban,
    label: "decision.status.dismissed",
    color: "var(--muted-foreground)",
  },
  awaiting: {
    tone: "awaiting",
    icon: UserRoundCheck,
    label: "decision.status.awaiting",
    color: "var(--review)",
  },
};

export function decisionTone(
  finding: Pick<ReviewFinding, "status" | "findingStatus">,
): DecisionTone {
  if (finding.status === "awaiting_decision") return "awaiting";
  return finding.findingStatus ?? "open";
}

export function decisionStyle(
  finding: Pick<ReviewFinding, "status" | "findingStatus">,
): DecisionStyle {
  return STYLES[decisionTone(finding)];
}

/** Only clauses with a verdict can be decided (docs/Phase 7 API.md). */
export function isDecidable(finding: Pick<ReviewFinding, "status" | "verdict">): boolean {
  return (
    finding.verdict !== null &&
    (finding.status === "done" || finding.status === "awaiting_decision")
  );
}

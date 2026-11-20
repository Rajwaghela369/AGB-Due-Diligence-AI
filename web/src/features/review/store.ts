// The review workspace's shared UI state. Document viewer, findings panel, table and
// drawer all read and write it, which is what keeps them in sync (Phase 6 plan, 6.D).
import { create } from "zustand";

export type FindingFilter = "void" | "review" | "all";
export type WorkspaceView = "document" | "table";
/** Narrow the findings by what the lawyer has done with them (Phase 7). */
export type StatusFilter = "all" | "open" | "accepted" | "overridden" | "dismissed";
export type DecisionAction = "accept" | "override" | "dismiss";

/**
 * A key press or card button asking the drawer to act on one finding. The drawer owns the
 * note and the reviewer dialog, so hotkeys only file a request; the drawer consumes it.
 */
export interface DecisionRequest {
  nonce: number;
  position: number;
  action: DecisionAction;
}

export interface ReviewUiState {
  /** Clause position the drawer shows and the document scrolls to. */
  selected: number | null;
  /** Clause position under the pointer in either panel; drives the cross-highlight. */
  hovered: number | null;
  filter: FindingFilter;
  statusFilter: StatusFilter;
  view: WorkspaceView;
  decisionRequest: DecisionRequest | null;
  /** Monotonic request counter: a consumed request never shares a nonce with a later one. */
  decisionSeq: number;
  /** Ordered positions the ◀ n / N ▶ control and J/K step through (set by the panel). */
  navigable: number[];
  /** Counter bumped by the `/` hotkey; the visible search box focuses itself when it changes. */
  focusSearch: number;
  select: (position: number | null) => void;
  hover: (position: number | null) => void;
  setFilter: (filter: FindingFilter) => void;
  setStatusFilter: (filter: StatusFilter) => void;
  /** Ask for a decision on `position` (default: the selected finding). */
  requestDecision: (action: DecisionAction, position?: number) => void;
  consumeDecision: (nonce: number) => void;
  setView: (view: WorkspaceView) => void;
  setNavigable: (positions: number[]) => void;
  requestSearchFocus: () => void;
  next: () => void;
  previous: () => void;
  reset: () => void;
}

const initial = {
  selected: null,
  hovered: null,
  filter: "all" as const,
  statusFilter: "all" as const,
  decisionRequest: null,
  decisionSeq: 0,
  view: "document" as const,
  navigable: [],
  focusSearch: 0,
};

function step(list: number[], current: number | null, delta: 1 | -1): number | null {
  if (list.length === 0) return null;
  const index = current === null ? -1 : list.indexOf(current);
  if (index === -1) return (delta === 1 ? list[0] : list[list.length - 1]) ?? null;
  return list[(index + delta + list.length) % list.length] ?? null;
}

export const useReviewUi = create<ReviewUiState>((set) => ({
  ...initial,
  select: (selected) => set({ selected }),
  hover: (hovered) => set({ hovered }),
  setFilter: (filter) => set({ filter }),
  setStatusFilter: (statusFilter) => set({ statusFilter }),
  requestDecision: (action, position) =>
    set((s) => {
      const target = position ?? s.selected;
      if (target === null) return {};
      const nonce = s.decisionSeq + 1;
      return { decisionSeq: nonce, decisionRequest: { nonce, position: target, action } };
    }),
  consumeDecision: (nonce) =>
    set((s) => (s.decisionRequest?.nonce === nonce ? { decisionRequest: null } : {})),
  setView: (view) => set({ view }),
  setNavigable: (navigable) => set({ navigable }),
  requestSearchFocus: () => set((s) => ({ focusSearch: s.focusSearch + 1 })),
  next: () => set((s) => ({ selected: step(s.navigable, s.selected, 1) })),
  previous: () => set((s) => ({ selected: step(s.navigable, s.selected, -1) })),
  reset: () => set(initial),
}));

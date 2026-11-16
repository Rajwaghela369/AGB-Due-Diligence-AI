// The review workspace's shared UI state. Document viewer, findings panel, table and
// drawer all read and write it, which is what keeps them in sync (Phase 6 plan, 6.D).
import { create } from "zustand";

export type FindingFilter = "void" | "review" | "all";
export type WorkspaceView = "document" | "table";

export interface ReviewUiState {
  /** Clause position the drawer shows and the document scrolls to. */
  selected: number | null;
  /** Clause position under the pointer in either panel; drives the cross-highlight. */
  hovered: number | null;
  filter: FindingFilter;
  view: WorkspaceView;
  /** Ordered positions the ◀ n / N ▶ control and J/K step through (set by the panel). */
  navigable: number[];
  select: (position: number | null) => void;
  hover: (position: number | null) => void;
  setFilter: (filter: FindingFilter) => void;
  setView: (view: WorkspaceView) => void;
  setNavigable: (positions: number[]) => void;
  next: () => void;
  previous: () => void;
  reset: () => void;
}

const initial = {
  selected: null,
  hovered: null,
  filter: "all" as const,
  view: "document" as const,
  navigable: [],
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
  setView: (view) => set({ view }),
  setNavigable: (navigable) => set({ navigable }),
  next: () => set((s) => ({ selected: step(s.navigable, s.selected, 1) })),
  previous: () => set((s) => ({ selected: step(s.navigable, s.selected, -1) })),
  reset: () => set(initial),
}));

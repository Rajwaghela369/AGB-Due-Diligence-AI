// Review progress from the per-review counts. These are the formulas of `ReviewOut` in
// docs/Phase 7 API.md; the findings endpoint and the stream carry `Review` (counts only), so
// the UI derives the same three values instead of waiting for a field they do not send.
import type { Review } from "../../api/types.gen";

export interface Progress {
  decidable: number;
  decided: number;
  openVoid: number;
  openReview: number;
  awaiting: number;
  /** decided / decidable, 0 when nothing can be decided yet. */
  fraction: number;
  canComplete: boolean;
}

export function progressOf(review: Pick<Review, "counts" | "status">): Progress {
  const c = review.counts;
  const decidable = (c?.potentially_void ?? 0) + (c?.uncertain ?? 0) + (c?.valid ?? 0);
  const decided = c?.decided ?? 0;
  const openVoid = c?.open_void ?? 0;
  const openReview = c?.open_review ?? 0;
  return {
    decidable,
    decided,
    openVoid,
    openReview,
    awaiting: c?.awaiting_decision ?? 0,
    fraction: decidable === 0 ? 0 : Math.min(decided / decidable, 1),
    canComplete:
      (review.status === "finished" || review.status === "awaiting_decisions") &&
      openVoid === 0 &&
      openReview === 0,
  };
}

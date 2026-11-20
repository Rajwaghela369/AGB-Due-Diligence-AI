// Recording a decision. The response carries the finding and the review (new counts), which
// replace the cached findings at once, so the card, table, drawer and progress bar change
// without a reload. The audit list and the home figures are refetched.
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { ApiError, api } from "../../api/client";
import type { DecisionIn, FindingsOut } from "../../api/types.gen";
import i18n from "../../i18n";
import { reviewKeys, useDecisionCount } from "../review/use-review";
import { useReviewer } from "./reviewer";

export const auditKey = (reviewId: string, position: number) =>
  ["reviews", reviewId, "audit", position] as const;

export function decisionErrorMessage(error: unknown): string {
  // The API's messages are German and meant to be shown as is (docs/Phase 7 API.md).
  if (error instanceof ApiError && error.body.message) return error.body.message;
  return i18n.t("decision.error.generic");
}

export function useDecide(reviewId: string) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const decide = useCallback(
    async (position: number, input: DecisionIn): Promise<boolean> => {
      // Wait for the reviewer name before any request; a closed dialog cancels the decision.
      const reviewer = await useReviewer.getState().ensure();
      if (reviewer === null) return false;
      setPending(true);
      setError(null);
      try {
        const out = await api.reviews.decide(reviewId, position, input, reviewer);
        queryClient.setQueryData<FindingsOut>(reviewKeys.findings(reviewId), (data) =>
          data === undefined
            ? data
            : {
                ...data,
                review: out.review.review,
                findings: data.findings.map((f) =>
                  f.position === position ? { ...f, ...out.finding } : f,
                ),
              },
        );
        useDecisionCount.getState().bump(reviewId);
        void queryClient.invalidateQueries({ queryKey: ["reviews", reviewId, "audit"] });
        void queryClient.invalidateQueries({ queryKey: ["metrics", "agreement"] });
        void queryClient.invalidateQueries({ queryKey: ["documents"] });
        return true;
      } catch (reason) {
        setError(decisionErrorMessage(reason));
        return false;
      } finally {
        setPending(false);
      }
    },
    [queryClient, reviewId],
  );

  return { decide, pending, error, clearError: () => setError(null) };
}

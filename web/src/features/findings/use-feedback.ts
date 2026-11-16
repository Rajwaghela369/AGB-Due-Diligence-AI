// Feedback mutation: the vote shows at once in the cached findings (the same cache
// `useReview` reads), is rolled back if the post fails, and the cache is refetched after.
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { api } from "../../api/client";
import type { FeedbackIn, FindingsOut } from "../../api/types.gen";
import { reviewKeys } from "../review/use-review";

export function useFeedback(reviewId: string) {
  const queryClient = useQueryClient();
  const key = reviewKeys.findings(reviewId);
  return useMutation({
    mutationFn: (input: FeedbackIn) => api.feedback(input),
    onMutate: async (input) => {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<FindingsOut>(key);
      queryClient.setQueryData<FindingsOut>(key, (data) =>
        data === undefined
          ? data
          : {
              ...data,
              findings: data.findings.map((f) =>
                f.position === input.position
                  ? {
                      ...f,
                      feedback: {
                        ...f.feedback,
                        agent_version: f.feedback?.agent_version ?? data.review.agent_version,
                        feedback_id: f.feedback?.feedback_id ?? "pending",
                        position: input.position,
                        review_id: input.review_id,
                        vote: input.vote,
                        reasons: input.reasons ?? [],
                        comment: input.comment ?? "",
                        corrected_verdict: input.corrected_verdict ?? null,
                        corrected_basis: input.corrected_basis ?? [],
                      },
                    }
                  : f,
              ),
            },
      );
      return { previous };
    },
    onError: (_error, _input, context) => {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: key }),
  });
}

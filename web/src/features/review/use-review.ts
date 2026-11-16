// The one source of review data for every workspace component.
//
// Stored findings (GET /reviews/{id}/findings) are the reload path: they rebuild the
// workspace with no new LLM calls. The live stream overlays clause status and verdicts
// while a review runs. Components never fetch findings or open streams themselves.
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api } from "../../api/client";
import { useReviewStream } from "../../api/events";
import type { ClauseSpan, ClauseVerdict, Finding, Review, Status1 } from "../../api/types.gen";

export interface ReviewFinding {
  position: number;
  clause: ClauseSpan;
  status: Status1;
  verdict: ClauseVerdict | null;
  error: string | null;
  decision: Finding["decision"];
  feedback: Finding["feedback"];
}

export interface ReviewData {
  review: Review | null;
  /** Every clause of the document, in document order, with live status. */
  findings: ReviewFinding[];
  byPosition: Map<number, ReviewFinding>;
  isLoading: boolean;
  error: Error | null;
  /** True once the stream reported RUN_FINISHED/RUN_ERROR or the stored review is final. */
  isFinal: boolean;
}

export const reviewKeys = {
  findings: (reviewId: string) => ["reviews", reviewId, "findings"] as const,
  document: (documentId: string) => ["documents", documentId] as const,
};

export function useReview(reviewId: string | undefined): ReviewData {
  const stored = useQuery({
    queryKey: reviewKeys.findings(reviewId ?? ""),
    queryFn: () => api.reviews.findings(reviewId as string),
    enabled: reviewId !== undefined,
  });
  const storedFinal =
    stored.data !== undefined && ["finished", "failed"].includes(stored.data.review.status);
  // A finished review is complete in storage; only an unfinished one needs the stream.
  const live = useReviewStream(storedFinal ? undefined : reviewId);

  return useMemo(() => {
    const base: ReviewFinding[] = (stored.data?.findings ?? []).map((f) => ({
      position: f.position,
      clause: f.clause,
      status: f.status,
      verdict: f.verdict ?? null,
      error: f.error ?? null,
      decision: f.decision,
      feedback: f.feedback,
    }));
    const clauses = live.state?.clauses ?? {};
    const findings = base.map((f) => {
      const entry = clauses[String(f.position)];
      return entry
        ? { ...f, status: entry.status, verdict: entry.verdict ?? f.verdict, error: entry.error }
        : f;
    });
    const review = live.state?.review ?? stored.data?.review ?? null;
    return {
      review,
      findings,
      byPosition: new Map(findings.map((f) => [f.position, f])),
      isLoading: stored.isLoading,
      error: (stored.error as Error | null) ?? live.error,
      isFinal: review !== null && ["finished", "failed"].includes(review.status),
    };
  }, [stored.data, stored.isLoading, stored.error, live.state, live.error]);
}

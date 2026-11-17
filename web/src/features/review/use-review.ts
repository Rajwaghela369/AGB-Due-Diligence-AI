// The one source of review data for every workspace component.
//
// Stored findings (GET /reviews/{id}/findings) are the reload path: they rebuild the
// workspace with no new LLM calls. The live stream overlays clause status and verdicts
// while a review runs. Components never fetch findings or open streams themselves.
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../../api/client";
import { useReviewStream, type ReviewProgress } from "../../api/events";
import type { ClauseSpan, ClauseVerdict, DocumentOut, Finding, Review } from "../../api/types.gen";
import type { ClauseStatus } from "../../api/aliases";

export type { ReviewProgress };

export interface ReviewFinding {
  position: number;
  clause: ClauseSpan;
  status: ClauseStatus;
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
  /** Latest `CUSTOM progress` value; null until the stream has sent one. */
  progress: ReviewProgress | null;
  /** The reviewed document (clauses, source type), fetched once and cached. */
  document: DocumentOut | null;
  /** Refetch the findings and reopen the stream, e.g. after retrying a clause. */
  refresh: () => Promise<void>;
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
  const streamCandidate = stored.data !== undefined && !storedFinal ? reviewId : undefined;
  const [streamReviewId, setStreamReviewId] = useState<string>();
  useEffect(() => {
    // Strict Mode replays mount effects. Deferring activation lets its cleanup cancel the
    // throwaway pass before useReviewStream can open a second connection.
    const timer = window.setTimeout(() => setStreamReviewId(streamCandidate), 0);
    return () => window.clearTimeout(timer);
  }, [streamCandidate]);
  const documentId = stored.data?.review.document_id;
  const documentQuery = useQuery({
    queryKey: reviewKeys.document(documentId ?? ""),
    queryFn: () => api.documents.get(documentId as string),
    enabled: documentId !== undefined,
    staleTime: Infinity,
  });
  // Stored findings decide whether live events are needed; never speculate before they load.
  const live = useReviewStream(streamReviewId);

  const { refetch } = stored;
  const { reopen } = live;
  const refresh = useCallback(async () => {
    await refetch();
    reopen();
  }, [refetch, reopen]);

  const documentData = documentQuery.data ?? null;
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
      progress: live.progress,
      document: documentData,
      refresh,
    };
  }, [
    stored.data,
    stored.isLoading,
    stored.error,
    live.state,
    live.error,
    live.progress,
    documentData,
    refresh,
  ]);
}

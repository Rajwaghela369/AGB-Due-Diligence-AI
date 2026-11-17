// The one source of review data for every workspace component.
//
// Stored findings (GET /reviews/{id}/findings) are the reload path: they rebuild the
// workspace with no new LLM calls. The live stream overlays clause status and verdicts
// while a review runs. Components never fetch findings or open streams themselves.
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import { create } from "zustand";

import { api } from "../../api/client";
import { useReviewStream, type ReviewProgress } from "../../api/events";
import type {
  ClauseSpan,
  ClauseVerdict,
  DocumentOut,
  Finding,
  FindingStatus,
  Review,
  ReviewOut,
  ReviewSummary,
} from "../../api/types.gen";
import type { ClauseStatus } from "../../api/aliases";

export type { ReviewProgress };

export interface ReviewFinding {
  position: number;
  clause: ClauseSpan;
  status: ClauseStatus;
  verdict: ClauseVerdict | null;
  error: string | null;
  decision: Finding["decision"];
  /** What the lawyer did with it: open until a decision exists (docs/Phase 7 API.md). */
  findingStatus: FindingStatus;
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
  pricing?: ReviewOut["pricing"];
  costEur?: ReviewOut["cost_eur"];
  wallTimeMs?: ReviewOut["wall_time_ms"];
  cacheHitRate?: ReviewOut["cache_hit_rate"];
  summary?: ReviewSummary | null;
  /** Refetch the findings and reopen the stream, e.g. after retrying a clause. */
  refresh: () => Promise<void>;
}

/**
 * How many decisions this page has recorded per review. A decision response is newer than any
 * stream state received before it, and the stream may already be closed (a review awaiting
 * decisions has no run), so `useReview` stops trusting the older stream state after one.
 */
export const useDecisionCount = create<{
  counts: Record<string, number>;
  bump: (reviewId: string) => void;
}>((set) => ({
  counts: {},
  bump: (reviewId) =>
    set((s) => ({ counts: { ...s.counts, [reviewId]: (s.counts[reviewId] ?? 0) + 1 } })),
}));

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
  // Findings are the reload path; accounting is deliberately returned by ReviewOut instead.
  const details = useQuery({
    queryKey: ["reviews", reviewId ?? "", "details"],
    queryFn: () => api.reviews.get(reviewId as string),
    enabled: reviewId !== undefined,
  });
  // Stored findings decide whether live events are needed; never speculate before they load.
  const live = useReviewStream(streamReviewId);

  const { refetch } = stored;
  const storedUpdatedAt = stored.dataUpdatedAt;
  const { refetch: refetchDetails } = details;
  const { reopen } = live;
  const refresh = useCallback(async () => {
    await refetch();
    reopen();
  }, [refetch, reopen]);

  const documentData = documentQuery.data ?? null;
  const decisions = useDecisionCount((s) => s.counts[reviewId ?? ""] ?? 0);
  // A terminal stream state is the RUN_FINISHED/RUN_ERROR refresh boundary for derived data.
  const streamStatus = live.state?.review.status;
  const summary = useQuery({
    queryKey: ["reviews", reviewId ?? "", "summary", decisions, streamStatus],
    queryFn: () => api.reviews.summary(reviewId as string),
    enabled: reviewId !== undefined && stored.data !== undefined,
    retry: false,
  });
  useEffect(() => {
    // Cost and wall time move whenever a run ends or a decision resumes it.
    if (decisions > 0) void refetchDetails();
  }, [refetchDetails, decisions]);
  useEffect(() => {
    if (
      streamStatus === "finished" ||
      streamStatus === "failed" ||
      streamStatus === "awaiting_decisions"
    )
      void refetchDetails();
  }, [refetchDetails, streamStatus]);
  const [stale, setStale] = useState<{ decisions: number; state: unknown }>({
    decisions: 0,
    state: null,
  });
  if (stale.decisions !== decisions) setStale({ decisions, state: live.state });
  // Live state received before the latest decision is older than the stored findings.
  const liveState = live.state !== null && live.state !== stale.state ? live.state : null;
  return useMemo(() => {
    const base: ReviewFinding[] = (stored.data?.findings ?? []).map((f) => ({
      position: f.position,
      clause: f.clause,
      status: f.status,
      verdict: f.verdict ?? null,
      error: f.error ?? null,
      decision: f.decision,
      findingStatus: f.finding_status ?? "open",
      feedback: f.feedback,
    }));
    const clauses = liveState?.clauses ?? {};
    const findings = base.map((f) => {
      const entry = clauses[String(f.position)];
      return entry
        ? { ...f, status: entry.status, verdict: entry.verdict ?? f.verdict, error: entry.error }
        : f;
    });
    // A decision writes `stored` directly; keep it unless the details were fetched after it.
    const detailsNewer = details.data !== undefined && details.dataUpdatedAt >= storedUpdatedAt;
    const review =
      liveState?.review ??
      (detailsNewer ? details.data?.review : undefined) ??
      stored.data?.review ??
      details.data?.review ??
      null;
    return {
      review,
      findings,
      byPosition: new Map(findings.map((f) => [f.position, f])),
      isLoading: stored.isLoading,
      error: (stored.error as Error | null) ?? live.error,
      isFinal:
        review !== null && ["finished", "awaiting_decisions", "failed"].includes(review.status),
      progress: live.progress,
      document: documentData,
      pricing: details.data?.pricing,
      costEur: details.data?.cost_eur,
      wallTimeMs: details.data?.wall_time_ms,
      cacheHitRate: details.data?.cache_hit_rate,
      summary: summary.data ?? null,
      refresh,
    };
  }, [
    stored.data,
    stored.isLoading,
    stored.error,
    liveState,
    live.error,
    live.progress,
    documentData,
    details.data,
    details.dataUpdatedAt,
    storedUpdatedAt,
    summary.data,
    refresh,
  ]);
}

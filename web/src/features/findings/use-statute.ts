// Statute text by chunk id. Cards and the drawer share one cache entry per chunk, so the
// strongest § on a card and the same § in the drawer cost a single request.
import { useQuery } from "@tanstack/react-query";

import { api } from "../../api/client";

export const statuteKey = (chunkId: string) => ["statutes", chunkId] as const;

export function useStatute(chunkId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: statuteKey(chunkId ?? ""),
    queryFn: () => api.statute(chunkId as string),
    enabled: enabled && chunkId !== undefined,
    staleTime: Infinity,
  });
}

/** The cited chunk with the highest basis score, falling back to the first cited. */
export function strongestBasis(
  legalBasis: string[] | undefined,
  scores: Record<string, number> | undefined,
): string | undefined {
  if (!legalBasis?.length) return undefined;
  return [...legalBasis].sort((a, b) => (scores?.[b] ?? 0) - (scores?.[a] ?? 0))[0];
}

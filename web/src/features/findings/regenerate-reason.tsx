// „Begründung erzeugen" for a void verdict whose reasoning failed (UI fix plan, bug 12).
// It replaces the dead-end notice in the drawer: one click re-runs only the explanation step
// (one model call on the server) and the stored findings are refetched, so the reasoning
// shows here, on the card and after a reload. A failure keeps the notice and offers a retry.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError, api } from "../../api/client";
import { reviewKeys } from "../review/use-review";

export function RegenerateReason({ reviewId, position }: { reviewId: string; position: number }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => api.reviews.explainClause(reviewId, position),
    onSettled: () => queryClient.invalidateQueries({ queryKey: reviewKeys.findings(reviewId) }),
  });
  const conflict = mutation.error instanceof ApiError && mutation.error.status === 409;

  return (
    <div className="space-y-2" data-testid="regenerate-reason">
      <p className="text-sm leading-relaxed text-muted-foreground">
        {t("drawer.reasonUnavailable")}
      </p>
      <button
        type="button"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
        className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {mutation.isPending ? (
          <Loader2 className="size-4 animate-spin" aria-hidden />
        ) : (
          <RefreshCw className="size-4" aria-hidden />
        )}
        {mutation.isPending ? t("drawer.regenerating") : t("drawer.regenerate")}
      </button>
      {mutation.isError && !conflict && (
        <p role="alert" className="text-sm">
          {t("drawer.regenerateError")}
        </p>
      )}
    </div>
  );
}

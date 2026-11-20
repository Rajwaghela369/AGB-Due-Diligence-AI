// The side panel: finding cards in document order behind Void / Review / All filters.
// It publishes the visible positions as `navigable`, so the viewer's ◀ ▶ and J/K walk
// exactly the list the lawyer is looking at (Phase 6 plan, 6.D).
import { useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import { toneOf } from "../review/risk";
import { useReviewUi, type FindingFilter, type StatusFilter } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import { ActivityFeed } from "./activity-feed";
import { FindingCard } from "./finding-card";
import { AvvObligations } from "../workspace/avv-obligations";

const FILTERS: FindingFilter[] = ["void", "review", "all"];
const STATUS_FILTERS: StatusFilter[] = ["all", "open", "accepted", "overridden", "dismissed"];

function matches(finding: ReviewFinding, filter: FindingFilter, status: StatusFilter): boolean {
  const tone = toneOf(finding);
  if (tone === "out") return false;
  if (status !== "all" && finding.findingStatus !== status) return false;
  return filter === "all" || tone === filter;
}

/** Findings cards in document order with Void / Review / All filters (6.D). */
export function FindingsPanel({ review, onOpen }: { review: ReviewData; onOpen?: () => void }) {
  const { t } = useTranslation();
  const filter = useReviewUi((s) => s.filter);
  const setFilter = useReviewUi((s) => s.setFilter);
  const statusFilter = useReviewUi((s) => s.statusFilter);
  const setStatusFilter = useReviewUi((s) => s.setStatusFilter);
  const setNavigable = useReviewUi((s) => s.setNavigable);
  const selected = useReviewUi((s) => s.selected);
  const listRef = useRef<HTMLDivElement>(null);

  const ordered = useMemo(
    () => [...review.findings].sort((a, b) => a.position - b.position),
    [review.findings],
  );
  const visible = useMemo(
    () => ordered.filter((f) => matches(f, filter, statusFilter)),
    [ordered, filter, statusFilter],
  );
  const counts = useMemo(
    () =>
      Object.fromEntries(
        FILTERS.map((f) => [f, ordered.filter((x) => matches(x, f, statusFilter)).length]),
      ) as Record<FindingFilter, number>,
    [ordered, statusFilter],
  );

  const key = visible.map((f) => f.position).join(",");
  useEffect(() => {
    setNavigable(key === "" ? [] : key.split(",").map(Number));
  }, [key, setNavigable]);

  useEffect(() => {
    if (selected === null) return;
    const card = listRef.current?.querySelector(`[data-testid="finding-card-${selected}"]`);
    // Only the list's own scroller moves; the page never scrolls.
    card?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);

  const reviewId = review.review?.review_id ?? "";

  return (
    <aside
      data-testid="findings-panel"
      aria-label={t("findings.title")}
      className="queue-panel flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-3"
    >
      {review.review?.document_type === "avv" && <AvvObligations review={review} />}
      <div className="space-y-3">
        <div>
          <h2 className="text-base font-semibold tracking-tight">{t("findings.title")}</h2>
        </div>
        <div role="group" aria-label={t("findings.filter")} className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
              className={cn(
                "rounded-md border border-border px-2.5 py-1.5 text-xs",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                filter === f ? "bg-foreground text-background" : "hover:bg-muted",
              )}
            >
              {t(`findings.filters.${f}`)} <span>{counts[f]}</span>
            </button>
          ))}
        </div>
      </div>

      <label className="flex items-center gap-3 border-y border-border py-3 text-sm text-muted-foreground">
        {t("decision.statusFilter")}
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
          className="h-9 rounded-md border border-border bg-background px-2 text-sm text-foreground"
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {t(`decision.statusFilters.${value}`)}
            </option>
          ))}
        </select>
      </label>

      <div ref={listRef} className="min-h-0 flex-1 space-y-2 overflow-y-auto">
        {visible.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">
            {review.isFinal ? t("findings.empty") : t("findings.waiting")}
          </p>
        ) : (
          visible.map((finding) => (
            <FindingCard
              key={finding.position}
              finding={finding}
              reviewId={reviewId}
              onOpen={onOpen}
            />
          ))
        )}
      </div>

      <ActivityFeed findings={review.findings} />
    </aside>
  );
}

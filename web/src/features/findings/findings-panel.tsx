// The side panel: finding cards in document order behind Void / Review / All filters.
// It publishes the visible positions as `navigable`, so the viewer's ◀ ▶ and J/K walk
// exactly the list the lawyer is looking at (Phase 6 plan, 6.D).
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";

import { cn } from "../../lib/utils";
import { toneOf } from "../review/risk";
import { useReviewUi, type FindingFilter, type StatusFilter } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import { ActivityFeed } from "./activity-feed";
import { FindingCard } from "./finding-card";

const FILTERS: FindingFilter[] = ["void", "review", "all"];
const STATUS_FILTERS: StatusFilter[] = ["all", "open", "accepted", "overridden", "dismissed"];

function matches(finding: ReviewFinding, filter: FindingFilter, status: StatusFilter): boolean {
  const tone = toneOf(finding);
  if (tone === "out") return false;
  if (status !== "all" && finding.findingStatus !== status) return false;
  return filter === "all" || tone === filter;
}

/** Findings cards in document order with Void / Review / All filters (6.D). */
export function FindingsPanel({ review }: { review: ReviewData }) {
  const { t } = useTranslation();
  const filter = useReviewUi((s) => s.filter);
  const setFilter = useReviewUi((s) => s.setFilter);
  const statusFilter = useReviewUi((s) => s.statusFilter);
  const setStatusFilter = useReviewUi((s) => s.setStatusFilter);
  const setNavigable = useReviewUi((s) => s.setNavigable);
  const selected = useReviewUi((s) => s.selected);
  // The drawer takes over this column while a clause is open (master/detail).
  const detailOpen = selected !== null && review.byPosition.has(selected);
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

  // display:none forgets the scroll position, so keep it and put it back with the list.
  const scrollTop = useRef(0);
  useLayoutEffect(() => {
    if (!detailOpen && listRef.current) listRef.current.scrollTop = scrollTop.current;
  }, [detailOpen]);

  useEffect(() => {
    if (selected === null || detailOpen) return;
    const card = listRef.current?.querySelector(`[data-testid="finding-card-${selected}"]`);
    // Only the list's own scroller moves; the page never scrolls.
    card?.scrollIntoView?.({ block: "nearest" });
  }, [selected, detailOpen]);

  const reviewId = review.review?.review_id ?? "";

  return (
    <aside
      data-testid="findings-panel"
      aria-label={t("findings.title")}
      className={cn("min-h-0 flex-1 flex-col gap-3", detailOpen ? "hidden" : "flex")}
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold tracking-wide">{t("findings.title")}</h2>
        <div role="group" aria-label={t("findings.filter")} className="flex gap-1">
          {FILTERS.map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={filter === f}
              onClick={() => setFilter(f)}
              className={cn(
                "rounded-md border border-border px-2 py-1 text-xs",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                filter === f ? "bg-foreground text-background" : "hover:bg-muted",
              )}
            >
              {t(`findings.filters.${f}`)} <span>{counts[f]}</span>
            </button>
          ))}
        </div>
      </div>

      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        {t("decision.statusFilter")}
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
          className="h-8 rounded-md border border-border bg-surface px-2 text-sm text-foreground"
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {t(`decision.statusFilters.${value}`)}
            </option>
          ))}
        </select>
      </label>

      <div
        ref={listRef}
        onScroll={(e) => {
          scrollTop.current = e.currentTarget.scrollTop;
        }}
        className="min-h-0 flex-1 space-y-2 overflow-y-auto"
      >
        {visible.length === 0 ? (
          <p className="p-3 text-sm text-muted-foreground">
            {review.isFinal ? t("findings.empty") : t("findings.waiting")}
          </p>
        ) : (
          visible.map((finding) => (
            <FindingCard key={finding.position} finding={finding} reviewId={reviewId} />
          ))
        )}
      </div>

      <ActivityFeed findings={review.findings} />
    </aside>
  );
}

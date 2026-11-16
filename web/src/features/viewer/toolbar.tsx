// Search, ◀ n / N ▶ through the findings, and zoom. Navigation steps through
// `useReviewUi.navigable` (set by the findings panel), so the toolbar, the panel and J/K
// always agree on what "next" means.
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ChevronUp,
  Minus,
  Plus,
  Search,
} from "lucide-react";
import { useEffect, useRef, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import { useReviewUi } from "../review/store";
import { ZOOM_MAX, ZOOM_MIN, zoomIn, zoomOut } from "./geometry";

const iconButton =
  "inline-flex size-8 items-center justify-center rounded-md text-foreground hover:bg-muted " +
  "focus-visible:outline-2 focus-visible:outline-ring disabled:opacity-40 disabled:hover:bg-transparent";

export function ViewerToolbar(props: {
  query: string;
  onQuery: (query: string) => void;
  hitCount: number;
  activeHit: number;
  onHit: (delta: 1 | -1) => void;
  zoom: number;
  onZoom: (zoom: number) => void;
}) {
  const { t } = useTranslation();
  const navigable = useReviewUi((s) => s.navigable);
  const selected = useReviewUi((s) => s.selected);
  const next = useReviewUi((s) => s.next);
  const previous = useReviewUi((s) => s.previous);
  const index = selected === null ? -1 : navigable.indexOf(selected);
  const focusSearch = useReviewUi((s) => s.focusSearch);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusSearch > 0) {
      input.current?.focus();
      input.current?.select();
    }
  }, [focusSearch]);
  const searching = props.query.trim().length >= 2;

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      props.onHit(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape" && props.query) {
      event.preventDefault();
      event.stopPropagation();
      props.onQuery("");
    }
  };

  return (
    <div
      role="toolbar"
      aria-label={t("viewer.toolbar")}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border bg-surface px-3 py-2 text-sm"
    >
      <div className="flex items-center gap-1">
        <label className="flex items-center gap-2 rounded-md border border-border bg-background px-2 focus-within:outline-2 focus-within:outline-ring">
          <Search aria-hidden="true" size={14} className="text-muted-foreground" />
          <span className="sr-only">{t("viewer.search.label")}</span>
          <input
            ref={input}
            type="search"
            value={props.query}
            placeholder={t("viewer.search.placeholder")}
            className="h-8 w-44 bg-transparent outline-none"
            onChange={(event) => props.onQuery(event.target.value)}
            onKeyDown={onKeyDown}
          />
        </label>
        <span aria-live="polite" className="min-w-20 text-xs text-muted-foreground tabular-nums">
          {searching &&
            (props.hitCount === 0
              ? t("viewer.search.none")
              : t("viewer.search.position", {
                  current: props.activeHit + 1,
                  count: props.hitCount,
                }))}
        </span>
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.search.previous")}
          disabled={props.hitCount === 0}
          onClick={() => props.onHit(-1)}
        >
          <ChevronUp aria-hidden="true" size={16} />
        </button>
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.search.next")}
          disabled={props.hitCount === 0}
          onClick={() => props.onHit(1)}
        >
          <ChevronDown aria-hidden="true" size={16} />
        </button>
      </div>

      <div className="flex items-center gap-1" role="group" aria-label={t("viewer.nav.label")}>
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.nav.previous")}
          disabled={navigable.length === 0}
          onClick={previous}
        >
          <ChevronLeft aria-hidden="true" size={16} />
        </button>
        <span className="min-w-14 text-center tabular-nums" data-testid="finding-position">
          {t("viewer.nav.position", {
            current: index === -1 ? "–" : index + 1,
            total: navigable.length,
          })}
        </span>
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.nav.next")}
          disabled={navigable.length === 0}
          onClick={next}
        >
          <ChevronRight aria-hidden="true" size={16} />
        </button>
      </div>

      <div
        className="ml-auto flex items-center gap-1"
        role="group"
        aria-label={t("viewer.zoom.label")}
      >
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.zoom.out")}
          disabled={props.zoom <= ZOOM_MIN}
          onClick={() => props.onZoom(zoomOut(props.zoom))}
        >
          <Minus aria-hidden="true" size={16} />
        </button>
        <button
          type="button"
          className="h-8 min-w-14 rounded-md px-2 tabular-nums hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
          aria-label={t("viewer.zoom.reset", { percent: Math.round(props.zoom * 100) })}
          onClick={() => props.onZoom(1)}
        >
          {Math.round(props.zoom * 100)}%
        </button>
        <button
          type="button"
          className={iconButton}
          aria-label={t("viewer.zoom.in")}
          disabled={props.zoom >= ZOOM_MAX}
          onClick={() => props.onZoom(zoomIn(props.zoom))}
        >
          <Plus aria-hidden="true" size={16} />
        </button>
      </div>
    </div>
  );
}

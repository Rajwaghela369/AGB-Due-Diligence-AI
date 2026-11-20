/** The home screen keeps stored reviews useful without opening a second review run. */
import { useQueries, useQuery } from "@tanstack/react-query";
import { ArrowUpRight, FileText, Play, Plus, RefreshCw, X } from "lucide-react";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../../api/client";
import type { ClauseVerdict, DocumentListItem, Review } from "../../api/types.gen";
import { Button } from "../../components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../../components/ui/dialog";
import { AgreementCard } from "../decisions/agreement-card";
import { ReviewCost } from "../cost/review-cost";
import { ProgressBar } from "../decisions/review-progress";
import { progressOf } from "../decisions/progress";
import { reviewProgress } from "../viewer/layout";
import { riskStyle } from "../review/risk";

const VERDICTS = ["potentially_void", "uncertain", "valid"] as const;
const riskColumns = VERDICTS.map((verdict) => ({
  verdict,
  ...riskStyle({ status: "done", verdict: { verdict } as ClauseVerdict }),
}));

const POLL_MS = 3000;

export function HomePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const documents = useQuery({
    queryKey: ["documents"],
    queryFn: api.documents.list,
  });

  if (documents.isLoading) return <p aria-busy="true">{t("home.loading")}</p>;
  if (documents.error) return <p role="alert">{t("home.loadError")}</p>;
  const items = documents.data?.items ?? [];

  return (
    <section data-testid="home-page" className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight">{t("home.title")}</h1>
          <p className="mt-2 text-muted-foreground">{t("home.description")}</p>
        </div>
        <Button type="button" onClick={() => navigate("/upload")}>
          <Plus aria-hidden="true" size={18} />
          {t("home.newReview")}
        </Button>
      </header>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border bg-surface p-10 text-center">
          <FileText aria-hidden="true" className="mx-auto mb-3 text-muted-foreground" size={30} />
          <h2 className="font-semibold">{t("home.empty.title")}</h2>
          <p className="mt-2 text-sm text-muted-foreground">{t("home.empty.description")}</p>
          <Button className="mt-5" type="button" onClick={() => navigate("/upload")}>
            <Plus aria-hidden="true" size={18} />
            {t("home.newReview")}
          </Button>
        </div>
      ) : (
        <DocumentTable items={items} pricing={documents.data?.pricing} />
      )}
      <AgreementCard />
    </section>
  );
}

const FILTERS = ["all", "active", "decisions", "finished", "failed", "unreviewed"] as const;
type ReviewSort = "date" | "name" | "status";

function DocumentTable({
  items,
  pricing,
}: {
  items: DocumentListItem[];
  pricing: import("../../api/types.gen").Pricing | undefined;
}) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  useLayoutEffect(() => {
    const saved = window.history.state?.libraryScrollY;
    if (typeof saved === "number" && saved > 0) window.scrollTo(0, saved);
  }, []);
  const query = params.get("q") ?? "";
  const requested = params.get("status");
  const filter = FILTERS.find((value) => value === requested) ?? "all";
  const requestedSort = params.get("sort");
  const sort = requestedSort === "name" || requestedSort === "status" ? requestedSort : "date";
  const desc = params.get("dir") ? params.get("dir") === "desc" : sort === "date";
  const update = (fields: Record<string, string>) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(fields)) {
          if (value) next.set(key, value);
          else next.delete(key);
        }
        return next;
      },
      { replace: true, preventScrollReset: true },
    );
  const sortBy = (field: ReviewSort) =>
    update({
      sort: field,
      dir: (sort === field ? !desc : field === "date") ? "desc" : "asc",
    });
  const sortDirection = (field: ReviewSort) =>
    sort === field ? (desc ? "descending" : "ascending") : "none";
  const liveReviews = items.flatMap((i) =>
    i.latest_review && ["queued", "running"].includes(i.latest_review.status)
      ? [i.latest_review]
      : [],
  );
  const polls = useQueries({
    queries: liveReviews.map((r) => ({
      queryKey: ["reviews", r.review_id, "status"],
      queryFn: () => api.reviews.get(r.review_id),
      refetchInterval: (q: { state: { data?: { review: Review } | undefined } }) =>
        ["running", "queued"].includes(q.state.data?.review.status ?? r.status)
          ? POLL_MS
          : (false as const),
    })),
  });
  const current = new Map(
    polls.flatMap((p) => (p.data ? [[p.data.review.review_id, p.data] as const] : [])),
  );
  const needle = query.trim().toLocaleLowerCase();
  const filteredItems = items
    .map((item) => {
      const live = current.get(item.latest_review?.review_id ?? "");
      return live
        ? {
            ...item,
            latest_review: live.review,
            ...(live.cost_eur !== undefined && { cost_eur: live.cost_eur }),
            ...(live.wall_time_ms !== undefined && { wall_time_ms: live.wall_time_ms }),
            ...(live.cache_hit_rate !== undefined && { cache_hit_rate: live.cache_hit_rate }),
          }
        : item;
    })
    .filter((item) => {
      const status = item.latest_review?.status;
      const matchesQuery =
        needle === "" || item.document.filename.toLocaleLowerCase().includes(needle);
      const matchesFilter =
        filter === "all" ||
        (filter === "unreviewed" && !status) ||
        (filter === "failed" && status === "failed") ||
        (filter === "active" && (status === "queued" || status === "running")) ||
        (filter === "decisions" && status === "awaiting_decisions") ||
        (filter === "finished" && status === "finished");
      return matchesQuery && matchesFilter;
    })
    .sort((a, b) => {
      if (sort === "name")
        return (desc ? -1 : 1) * a.document.filename.localeCompare(b.document.filename);
      if (sort === "status")
        return (
          (desc ? -1 : 1) *
          (a.latest_review?.status ?? "none").localeCompare(b.latest_review?.status ?? "none")
        );
      return (
        (desc ? -1 : 1) *
        ((Date.parse(a.document.created_at ?? "") || 0) -
          (Date.parse(b.document.created_at ?? "") || 0))
      );
    });

  return (
    <section
      className="overflow-hidden rounded-2xl border border-border bg-surface shadow-sm"
      aria-label={t("home.table.label")}
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-muted/30 px-5 py-4">
        <div>
          <h2 className="text-base font-semibold">{t("home.table.title")}</h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {t("home.table.count", { count: filteredItems.length })}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative">
            <span className="sr-only">{t("home.table.search")}</span>
            <input
              className="h-9 w-56 rounded-lg border border-border bg-background px-3 text-sm outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
              placeholder={t("home.table.search")}
              value={query}
              onChange={(event) => update({ q: event.target.value })}
            />
          </label>
          <label>
            <span className="sr-only">{t("home.table.status")}</span>
            <select
              className="h-9 rounded-lg border border-border bg-background px-3 text-sm"
              value={filter}
              onChange={(event) => update({ status: event.target.value })}
            >
              {FILTERS.map((value) => (
                <option key={value} value={value}>
                  {t(`home.table.filters.${value}`)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="library-table w-full min-w-[52rem] text-left text-sm">
          <thead className="border-b border-border bg-muted/40 text-xs uppercase tracking-[0.1em] text-muted-foreground">
            <tr>
              <th aria-sort={sortDirection("name")} className="w-[29%] px-5 py-3 font-semibold">
                <SortButton
                  active={sort === "name"}
                  descending={desc}
                  onClick={() => sortBy("name")}
                >
                  {t("home.table.document")}
                </SortButton>
              </th>
              <th aria-sort={sortDirection("date")} className="px-4 py-3">
                <SortButton
                  active={sort === "date"}
                  descending={desc}
                  onClick={() => sortBy("date")}
                >
                  {t("workspace.date")}
                </SortButton>
              </th>
              {riskColumns.map(({ verdict, label, icon: Icon, color }) => (
                <th
                  key={verdict}
                  scope="col"
                  className="px-2 py-3 text-center normal-case tracking-normal"
                >
                  <Icon aria-hidden size={15} style={{ color }} className="mx-auto mb-1" />
                  {t(label)}
                </th>
              ))}
              <th aria-sort={sortDirection("status")} className="w-[22%] px-5 py-3 font-semibold">
                <SortButton
                  active={sort === "status"}
                  descending={desc}
                  onClick={() => sortBy("status")}
                >
                  {t("table.status")}
                </SortButton>
              </th>
              <th className="px-4 py-3 font-semibold">{t("home.table.action")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filteredItems.map((item) => (
              <DocumentRow key={item.document.document_id} item={item} pricing={pricing} />
            ))}
          </tbody>
        </table>
      </div>
      {filteredItems.length === 0 && (
        <p className="border-t border-border px-5 py-10 text-center text-sm text-muted-foreground">
          {t("home.table.empty")}{" "}
          <button
            className="underline underline-offset-2"
            onClick={() => update({ q: "", status: "" })}
          >
            {t("home.table.reset")}
          </button>
        </p>
      )}
    </section>
  );
}

function SortButton({
  active,
  descending,
  onClick,
  children,
}: {
  active: boolean;
  descending: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className="rounded-md py-1 text-left hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      {children}
      <span aria-hidden="true" className="ml-1 text-[0.65rem]">
        {active ? (descending ? "↓" : "↑") : "↕"}
      </span>
    </button>
  );
}

function DocumentRow({
  item,
  pricing,
}: {
  item: DocumentListItem;
  pricing: import("../../api/types.gen").Pricing | undefined;
}) {
  const { i18n, t } = useTranslation();
  const navigate = useNavigate();
  const [scopeOpen, setScopeOpen] = useState(false);
  const [scopeTrigger, setScopeTrigger] = useState<HTMLButtonElement | null>(null);
  const { document, latest_review: review } = item;
  return (
    <tr className="group align-middle transition-colors hover:bg-muted/35">
      <td className="px-5 py-5">
        <button
          type="button"
          className="block max-w-full text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          disabled={!review}
          onClick={() => {
            if (!review) return;
            window.history.replaceState(
              { ...window.history.state, libraryScrollY: window.scrollY },
              "",
            );
            navigate(`/reviews/${review.review_id}`);
          }}
        >
          <span className="block">
            <span className="flex items-center gap-2 font-medium [overflow-wrap:anywhere]">
              <FileText className="shrink-0" aria-hidden="true" size={18} /> {document.filename}
              {review && (
                <ArrowUpRight aria-hidden size={14} className="shrink-0 text-muted-foreground" />
              )}
            </span>
          </span>
        </button>
      </td>
      <td data-testid="document-date" className="px-4 py-4 tabular-nums">
        {formatDate(document.created_at, i18n.language)}
      </td>
      {riskColumns.map(({ verdict, color }) => (
        <td
          key={verdict}
          className="px-2 py-4 text-center font-medium tabular-nums"
          style={{ color: review?.counts?.[verdict] ? color : undefined }}
        >
          {review ? (review.counts?.[verdict] ?? 0) : "—"}
        </td>
      ))}
      <td className="px-5 py-5">
        {review ? (
          <Progress review={review} />
        ) : (
          <span className="text-muted-foreground">{t("home.notReviewed")}</span>
        )}
      </td>
      <td className="px-5 py-5">
        {review ? (
          <details>
            <summary className="cursor-pointer text-sm">{t("home.table.usage")}</summary>
            <ReviewCost
              review={review}
              pricing={pricing}
              costEur={item.cost_eur}
              wallTimeMs={item.wall_time_ms}
              cacheHitRate={item.cache_hit_rate}
              compact
            />
          </details>
        ) : null}
        {!review ? (
          <Button
            type="button"
            onClick={(event) => {
              setScopeTrigger(event.currentTarget);
              setScopeOpen(true);
            }}
          >
            <Play aria-hidden="true" size={16} />
            {t("home.startReview")}
          </Button>
        ) : null}
        {scopeOpen ? (
          <StartReviewDialog
            documentId={document.document_id}
            filename={document.filename}
            onClose={() => setScopeOpen(false)}
            returnFocusTo={scopeTrigger}
          />
        ) : null}
      </td>
    </tr>
  );
}

function StartReviewDialog({
  documentId,
  filename,
  onClose,
  returnFocusTo,
}: {
  documentId: string;
  filename: string;
  onClose: () => void;
  returnFocusTo: HTMLButtonElement | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [liability, setLiability] = useState(true);
  const [warranty, setWarranty] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState(false);
  const open = useRef(true);
  const close = () => {
    open.current = false;
    onClose();
  };
  const start = async () => {
    setStarting(true);
    setError(false);
    try {
      const scope = [liability && "liability", warranty && "warranty"].filter(Boolean) as Array<
        "liability" | "warranty"
      >;
      const result = await api.reviews.start({ document_id: documentId, scope });
      if (open.current) navigate(`/reviews/${result.review.review_id}`);
    } catch {
      setError(true);
      setStarting(false);
    }
  };
  return (
    <Dialog
      open
      onOpenChange={(nextOpen) => {
        if (!nextOpen && !starting) close();
      }}
    >
      <DialogContent
        aria-labelledby="start-review-title"
        className="inset-1/2 z-50 h-fit w-[min(28rem,calc(100vw-2rem))] max-w-none -translate-x-1/2 -translate-y-1/2 rounded-xl border border-border bg-surface p-6 shadow-lg"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          returnFocusTo?.focus();
        }}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <DialogTitle id="start-review-title" className="text-lg font-semibold">
              {t("home.startReview")}
            </DialogTitle>
            <DialogDescription className="mt-1 text-sm text-muted-foreground">
              {filename}
            </DialogDescription>
          </div>
          <DialogClose asChild>
            <Button
              aria-label={t("home.closeDialog")}
              disabled={starting}
              size="icon"
              type="button"
              variant="ghost"
            >
              <X aria-hidden="true" size={18} />
            </Button>
          </DialogClose>
        </div>
        <p className="mt-4 text-sm text-muted-foreground">{t("home.aiCostHint")}</p>
        <fieldset className="mt-4 space-y-2">
          <legend className="font-medium">{t("home.scope")}</legend>
          <label className="flex items-center gap-2">
            <input
              checked={liability}
              className="accent-accent"
              type="checkbox"
              onChange={(event) => setLiability(event.target.checked)}
            />
            {t("upload.liability")}
          </label>
          <label className="flex items-center gap-2">
            <input
              checked={warranty}
              className="accent-accent"
              type="checkbox"
              onChange={(event) => setWarranty(event.target.checked)}
            />
            {t("upload.warranty")}
          </label>
        </fieldset>
        {error ? (
          <p role="alert" className="mt-4 text-sm text-risk">
            {t("home.startError")}
          </p>
        ) : null}
        <div className="mt-6 flex justify-end gap-2">
          <Button disabled={starting} type="button" variant="ghost" onClick={close}>
            {t("home.cancel")}
          </Button>
          <Button
            autoFocus
            disabled={starting || (!liability && !warranty)}
            type="button"
            onClick={() => void start()}
          >
            <Play aria-hidden="true" size={16} />
            {t("home.startReview")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Progress({ review }: { review: Review }) {
  const { t } = useTranslation();
  const { done, total } = reviewProgress(review);
  const running = review.status === "running";
  const decisions = progressOf(review);
  return (
    <div className="flex flex-col items-start gap-1 text-sm">
      <span className="flex items-center justify-end gap-2">
        {running ? <RefreshCw aria-hidden="true" className="animate-spin" size={16} /> : null}
        <span>{t(`home.status.${review.status}`)}</span>
      </span>
      <span className="text-xs tabular-nums text-muted-foreground">
        {t("home.progress", { done, total })}
      </span>
      {decisions.decidable > 0 && (
        <div className="flex w-full items-center" data-testid="home-decision-progress">
          <ProgressBar review={review} />
        </div>
      )}
    </div>
  );
}

function formatDate(value: string | null | undefined, language: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? "—"
    : new Intl.DateTimeFormat(language, {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      }).format(date);
}

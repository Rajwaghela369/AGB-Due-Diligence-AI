/** The home screen keeps stored reviews useful without opening a second review run. */
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, FileText, Play, Plus, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import type { ClauseVerdict, DocumentListItem, Review } from "../../api/types.gen";
import { Button } from "../../components/ui/button";
import { riskStyle } from "../review/risk";

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
        <ul className="divide-y divide-border rounded-xl border border-border bg-surface">
          {items.map((item) => (
            <DocumentRow key={item.document.document_id} item={item} />
          ))}
        </ul>
      )}
    </section>
  );
}

/** A running review polls its own status; there is no stream on the home screen. */
function DocumentRow({ item }: { item: DocumentListItem }) {
  const { i18n, t } = useTranslation();
  const navigate = useNavigate();
  const [scopeOpen, setScopeOpen] = useState(false);
  const { document, latest_review: stored } = item;
  const polled = useQuery({
    queryKey: ["reviews", stored?.review_id, "status"],
    queryFn: () => api.reviews.get(stored?.review_id as string),
    enabled: stored?.status === "running" || stored?.status === "queued",
    refetchInterval: (query) => {
      const status = query.state.data?.review.status ?? stored?.status;
      return status === "running" || status === "queued" ? POLL_MS : false;
    },
  });
  const review = polled.data?.review ?? stored;
  return (
    <li className="grid gap-4 p-5 sm:grid-cols-[minmax(12rem,1fr)_auto_auto] sm:items-center">
      <button
        type="button"
        className="contents text-left focus-visible:outline-none"
        disabled={!review}
        onClick={() => review && navigate(`/reviews/${review.review_id}`)}
      >
        <span>
          <span className="flex items-center gap-2 font-medium">
            <FileText aria-hidden="true" size={18} /> {document.filename}
          </span>
          <span className="mt-1 flex items-center gap-1 text-sm text-muted-foreground">
            <CalendarDays aria-hidden="true" size={15} />{" "}
            {formatDate(document.created_at, i18n.language)}
          </span>
        </span>
        {review ? <RiskCounts review={review} /> : <span>{t("home.notReviewed")}</span>}
        {review ? <Progress review={review} /> : null}
      </button>
      {!review ? (
        <Button type="button" onClick={() => setScopeOpen(true)}>
          <Play aria-hidden="true" size={16} />
          {t("home.startReview")}
        </Button>
      ) : null}
      {scopeOpen ? (
        <StartReviewDialog
          documentId={document.document_id}
          filename={document.filename}
          onClose={() => setScopeOpen(false)}
        />
      ) : null}
    </li>
  );
}

function StartReviewDialog({
  documentId,
  filename,
  onClose,
}: {
  documentId: string;
  filename: string;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [liability, setLiability] = useState(true);
  const [warranty, setWarranty] = useState(true);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState(false);
  const start = async () => {
    setStarting(true);
    setError(false);
    try {
      const scope = [liability && "liability", warranty && "warranty"].filter(Boolean) as Array<
        "liability" | "warranty"
      >;
      const result = await api.reviews.start({ document_id: documentId, scope });
      navigate(`/reviews/${result.review.review_id}`);
    } catch {
      setError(true);
      setStarting(false);
    }
  };
  return (
    <div
      aria-labelledby="start-review-title"
      aria-modal="true"
      className="fixed inset-0 z-50 grid place-items-center bg-foreground/20 p-4"
      role="dialog"
    >
      <div className="w-full max-w-md rounded-xl border border-border bg-surface p-6 shadow-lg">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="start-review-title" className="text-lg font-semibold">
              {t("home.startReview")}
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">{filename}</p>
          </div>
          <Button
            aria-label={t("home.closeDialog")}
            size="icon"
            type="button"
            variant="ghost"
            onClick={onClose}
          >
            <X aria-hidden="true" size={18} />
          </Button>
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
          <Button type="button" variant="ghost" onClick={onClose}>
            {t("home.cancel")}
          </Button>
          <Button
            disabled={starting || (!liability && !warranty)}
            type="button"
            onClick={() => void start()}
          >
            <Play aria-hidden="true" size={16} />
            {t("home.startReview")}
          </Button>
        </div>
      </div>
    </div>
  );
}

function RiskCounts({ review }: { review: Review }) {
  const { t } = useTranslation();
  const counts = review.counts;
  const entries = [
    ["potentially_void", counts?.potentially_void ?? 0],
    ["uncertain", counts?.uncertain ?? 0],
    ["valid", counts?.valid ?? 0],
  ] as const;
  return (
    <span className="flex flex-wrap gap-3 text-sm">
      {entries.map(([verdict, count]) => {
        const style = riskStyle({ status: "done", verdict: { verdict } as ClauseVerdict });
        const Icon = style.icon;
        return (
          <span key={verdict} className="flex items-center gap-1" style={{ color: style.color }}>
            <Icon aria-hidden="true" size={16} />
            <span>{t(style.label)}</span>
            <span className="sr-only">: </span>
            <span className="tabular-nums">{count}</span>
          </span>
        );
      })}
    </span>
  );
}

function Progress({ review }: { review: Review }) {
  const { t } = useTranslation();
  const done = (review.counts?.done ?? 0) + (review.counts?.failed ?? 0);
  const total = review.counts?.total ?? 0;
  const running = review.status === "running";
  return (
    <span className="flex items-center justify-end gap-2 text-sm">
      {running ? <RefreshCw aria-hidden="true" className="animate-spin" size={16} /> : null}
      <span>{t(`home.status.${review.status}`)}</span>
      <span className="tabular-nums text-muted-foreground">
        <span className="sr-only">{t("home.progress", { done, total })}</span>
        <span aria-hidden="true">
          {done}/{total}
        </span>
      </span>
    </span>
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

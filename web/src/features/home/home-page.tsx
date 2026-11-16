/** The home screen keeps stored reviews useful without opening a second review run. */
import { useQuery } from "@tanstack/react-query";
import { CalendarDays, FileText, Plus, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api } from "../../api/client";
import type { Review } from "../../api/types.gen";
import { Button } from "../../components/ui/button";
import { riskStyle } from "../review/risk";

const FINAL_STATUSES = new Set(["finished", "failed"]);

export function HomePage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const documents = useQuery({
    queryKey: ["documents"],
    queryFn: api.documents.list,
    refetchInterval: (query) =>
      query.state.data?.items.some((item) => item.latest_review?.status === "running") ? 3000 : false,
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
          {items.map(({ document, latest_review: review }) => (
            <li key={document.document_id}>
              <button
                type="button"
                className="grid w-full gap-4 p-5 text-left hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:grid-cols-[minmax(12rem,1fr)_auto_auto] sm:items-center"
                disabled={!review}
                onClick={() => review && navigate(`/reviews/${review.review_id}`)}
              >
                <span>
                  <span className="flex items-center gap-2 font-medium">
                    <FileText aria-hidden="true" size={18} /> {document.filename}
                  </span>
                  <span className="mt-1 flex items-center gap-1 text-sm text-muted-foreground">
                    <CalendarDays aria-hidden="true" size={15} /> {formatDate(document.created_at)}
                  </span>
                </span>
                {review ? <RiskCounts review={review} /> : <span>{t("home.notReviewed")}</span>}
                {review ? <Progress review={review} /> : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function RiskCounts({ review }: { review: Review }) {
  const { t } = useTranslation();
  const counts = review.counts;
  const entries = [
    ["potentially_void", counts?.potentially_void ?? 0, "potentially_void"],
    ["uncertain", counts?.uncertain ?? 0, "uncertain"],
    ["valid", counts?.valid ?? 0, "valid"],
  ] as const;
  return (
    <span className="flex flex-wrap gap-3 text-sm">
      {entries.map(([key, count, verdict]) => {
        const style = riskStyle({ status: "done", verdict: { verdict } } as never);
        const Icon = style.icon;
        return (
          <span key={key} className="flex items-center gap-1" style={{ color: style.color }}>
            <Icon aria-hidden="true" size={16} />
            <span>{t(style.label, { count })}</span>
            <span className="sr-only">: </span>{count}
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
    <span className="flex items-center justify-end gap-2 text-sm" aria-label={t("home.progress", { done, total })}>
      {running ? <RefreshCw aria-hidden="true" className="animate-spin" size={16} /> : null}
      <span>{running ? t("home.running") : FINAL_STATUSES.has(review.status) ? t("home.finished") : t("home.queued")}</span>
      <span className="tabular-nums text-muted-foreground">{done}/{total}</span>
    </span>
  );
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

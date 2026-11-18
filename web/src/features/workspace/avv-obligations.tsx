import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Obligation, ObligationFinding } from "../../api/types.gen";
import { cn } from "../../lib/utils";
import { useReviewUi } from "../review/store";
import type { ReviewData } from "../review/use-review";

type AvvFilter = "all" | "open" | "fulfilled";

const CODES: Obligation[] = ["a", "b", "c", "d", "e", "f", "g", "h"];

const LABELS: Record<Obligation, string> = {
  a: "Gegenstand und Dauer der Verarbeitung",
  b: "Art und Zweck der Verarbeitung",
  c: "Art der personenbezogenen Daten",
  d: "Kategorien betroffener Personen",
  e: "Pflichten und Rechte des Verantwortlichen",
  f: "Vertraulichkeit und Datenschutz",
  g: "Unterauftragsverarbeiter",
  h: "Unterstützung und Löschung/Rückgabe",
};

function label(code: Obligation, missingLabel?: string) {
  return missingLabel || `${code}) ${LABELS[code]}`;
}

function FindingRow({ finding, position }: { finding: ObligationFinding; position: number }) {
  const { t } = useTranslation();
  const select = useReviewUi((state) => state.select);
  return (
    <li className="rounded-md border border-border p-3 text-sm">
      <div className="flex items-start gap-2">
        <span
          className={cn(
            "mt-0.5 rounded-full px-2 py-0.5 text-xs font-medium",
            finding.fulfilled ? "bg-emerald-100 text-emerald-800" : "bg-amber-100 text-amber-900",
          )}
        >
          {finding.fulfilled ? t("avv.fulfilled") : t("avv.open")}
        </span>
        <span className="font-medium">
          {finding.obligation}) {LABELS[finding.obligation]}
        </span>
        <button
          type="button"
          className="ml-auto text-xs underline underline-offset-2"
          onClick={() => select(position)}
        >
          {t("avv.showClause")}
        </button>
      </div>
      <p className="mt-1 text-muted-foreground">{finding.reason_de || t("avv.noReason")}</p>
    </li>
  );
}

/** AVV-only obligation view. AGB reviews never render this component. */
export function AvvObligations({ review }: { review: ReviewData }) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<AvvFilter>("all");
  const [code, setCode] = useState<Obligation | "all">("all");
  const entries = useMemo(
    () =>
      review.findings.flatMap((finding) =>
        (finding.obligationFindings ?? []).map((obligation) => ({
          obligation,
          position: finding.position,
        })),
      ),
    [review.findings],
  );
  const missing = review.missingObligations ?? [];
  const visibleEntries = entries.filter(({ obligation }) => {
    if (code !== "all" && obligation.obligation !== code) return false;
    return (
      filter === "all" || (filter === "fulfilled" ? obligation.fulfilled : !obligation.fulfilled)
    );
  });
  const visibleMissing =
    filter === "fulfilled"
      ? []
      : missing.filter((item) => code === "all" || item.obligation.code === code);

  return (
    <section
      className="space-y-3 rounded-lg border border-border bg-surface p-3"
      data-testid="avv-obligations"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">{t("avv.title")}</h2>
          <p className="text-xs text-muted-foreground">{t("avv.description")}</p>
        </div>
        <span className="rounded-full border border-border px-2 py-1 text-xs">
          {t("avv.progress", { found: entries.length, missing: missing.length })}
        </span>
      </div>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t("avv.filter")}>
        {(["all", "open", "fulfilled"] as AvvFilter[]).map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
            className={cn(
              "rounded-md border border-border px-2 py-1 text-xs",
              filter === value && "bg-foreground text-background",
            )}
          >
            {t(`avv.filters.${value}`)}
          </button>
        ))}
        <label className="ml-auto flex items-center gap-1 text-xs text-muted-foreground">
          {t("avv.obligation")}
          <select
            aria-label={t("avv.obligation")}
            value={code}
            onChange={(event) => setCode(event.target.value as Obligation | "all")}
            className="h-7 rounded-md border border-border bg-surface px-1 text-foreground"
          >
            <option value="all">{t("avv.allObligations")}</option>
            {CODES.map((item) => (
              <option key={item} value={item}>
                {item}) {LABELS[item]}
              </option>
            ))}
          </select>
        </label>
      </div>
      {visibleMissing.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm">
          <h3 className="font-medium">{t("avv.missingTitle")}</h3>
          <ul className="mt-2 space-y-2">
            {visibleMissing.map((item) => (
              <li key={item.obligation.code}>
                <span className="font-medium">
                  {label(item.obligation.code, item.obligation.label_de)}
                </span>
                <span className="ml-2 text-muted-foreground">{item.reason_de}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {visibleEntries.length > 0 ? (
        <ul className="space-y-2">
          {visibleEntries.map(({ obligation, position }, index) => (
            <FindingRow
              key={`${position}-${obligation.obligation}-${index}`}
              finding={obligation}
              position={position}
            />
          ))}
        </ul>
      ) : visibleMissing.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("avv.empty")}</p>
      ) : null}
    </section>
  );
}

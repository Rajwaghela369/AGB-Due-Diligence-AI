import { Clock3, Database, Euro } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Pricing, Review } from "../../api/types.gen";
import { formatDuration, formatMoney } from "./format";

export function ReviewCost({
  review,
  pricing,
  costEur,
  wallTimeMs,
  cacheHitRate,
  compact = false,
}: {
  review: Review;
  pricing: Pricing | undefined;
  costEur: number | null | undefined;
  wallTimeMs: number | null | undefined;
  cacheHitRate: number | undefined;
  compact?: boolean;
}) {
  const { i18n, t } = useTranslation();
  const money = formatMoney(review.cost_usd, costEur, pricing, i18n.language);
  const items = [
    { icon: Euro, label: t("cost.reviewCost"), value: money ?? t("cost.unpriced") },
    { icon: Clock3, label: t("cost.wallTime"), value: formatDuration(wallTimeMs, i18n.language) },
    {
      icon: Database,
      label: t("cost.cacheShare"),
      value: new Intl.NumberFormat(i18n.language, {
        style: "percent",
        maximumFractionDigits: 0,
      }).format(cacheHitRate ?? 0),
    },
  ];
  return (
    <dl
      className={
        compact ? "flex flex-wrap gap-x-3 gap-y-1 text-xs" : "flex flex-wrap gap-3 text-sm"
      }
    >
      {items.map(({ icon: Icon, label, value }) => (
        <div key={label} className="flex items-center gap-1.5 text-muted-foreground">
          <Icon aria-hidden className="size-3.5" />
          <dt className="sr-only">{label}</dt>
          <dd title={label} className="tabular-nums">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

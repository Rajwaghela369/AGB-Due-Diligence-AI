// Lawyer agreement for the current agent version, on the home screen (7.C): the share of
// decided findings where the lawyer kept the model's verdict, with the counts behind it.
import { useQuery } from "@tanstack/react-query";
import { Scale } from "lucide-react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";

export function AgreementCard() {
  const { t } = useTranslation();
  const query = useQuery({
    queryKey: ["metrics", "agreement"],
    queryFn: () => api.metrics.agreement(),
  });
  if (query.isLoading) return null;
  const agreement = query.data?.agreement;
  const decided = agreement?.decided ?? 0;
  return (
    <section
      data-testid="agreement"
      aria-label={t("agreement.title")}
      className="rounded-xl border border-border bg-surface p-5"
    >
      <h2 className="flex items-center gap-2 text-sm font-semibold">
        <Scale aria-hidden="true" size={16} />
        {t("agreement.title")}
        {agreement && (
          <span className="font-normal text-muted-foreground">
            {t("agreement.version", { version: agreement.agent_version })}
          </span>
        )}
      </h2>
      {!agreement ? (
        <p className="mt-2 text-sm text-muted-foreground">{t("agreement.unavailable")}</p>
      ) : decided === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">{t("agreement.none")}</p>
      ) : (
        <div className="mt-2 flex flex-wrap items-baseline gap-x-6 gap-y-1">
          <p className="text-2xl font-semibold tabular-nums" data-testid="agreement-rate">
            {agreement.rate == null ? "—" : `${Math.round(agreement.rate * 100)} %`}
          </p>
          <ul className="flex flex-wrap gap-x-4 text-sm tabular-nums">
            <li>{t("agreement.decided", { count: decided })}</li>
            <li>{t("agreement.kept", { count: agreement.kept ?? 0 })}</li>
            <li>{t("agreement.overridden", { count: agreement.overridden ?? 0 })}</li>
            <li>{t("agreement.dismissed", { count: agreement.dismissed ?? 0 })}</li>
          </ul>
          <p className="basis-full text-xs text-muted-foreground">{t("agreement.explain")}</p>
        </div>
      )}
    </section>
  );
}

// „Verlauf“: the decision history of one finding, newest first (docs/Phase 7 API.md). The
// trail is append-only on the server; this view only reads it. It refetches after each
// decision (`useDecide` invalidates the key).
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import type { DecisionEvent } from "../../api/types.gen";
import { useStatute } from "../findings/use-statute";
import { auditKey } from "./use-decide";

function Basis({ ids }: { ids: string[] }) {
  return (
    <>
      {ids.length === 0
        ? "—"
        : ids.map((id, i) => <Citation key={id} id={id} last={i === ids.length - 1} />)}
    </>
  );
}

function Citation({ id, last }: { id: string; last: boolean }) {
  const statute = useStatute(id);
  return (
    <>
      {statute.data?.citation ?? id}
      {last ? "" : ", "}
    </>
  );
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function formatTime(value: string, language: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat(language, { dateStyle: "short", timeStyle: "short" }).format(date);
}

function Entry({ event }: { event: DecisionEvent }) {
  const { t, i18n } = useTranslation();
  const fromBasis = event.from_basis ?? [];
  const toBasis = event.to_basis ?? [];
  const verdictLabel = (value: string | null | undefined) =>
    value ? t(`decision.verdict.${value}`) : t("audit.noVerdict");
  const basisChanged = !sameList(fromBasis, toBasis);
  return (
    <li className="rounded-md border border-border p-2.5 text-sm" data-testid="audit-entry">
      <p className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{t(`audit.action.${event.action}`)}</span>
        <span>
          {event.decided_by ?? t("audit.unknown")}
          {!event.authenticated && (
            <span className="text-muted-foreground"> ({t("audit.unauthenticated")})</span>
          )}
        </span>
        <time className="ml-auto text-xs text-muted-foreground" dateTime={event.at}>
          {formatTime(event.at, i18n.language)}
        </time>
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {t("audit.verdict")}: {verdictLabel(event.from_verdict)} → {verdictLabel(event.to_verdict)}
      </p>
      {basisChanged && (
        <p className="text-xs text-muted-foreground">
          {t("audit.basis")}: <Basis ids={fromBasis} /> → <Basis ids={toBasis} />
        </p>
      )}
      {event.note ? <p className="mt-1">{event.note}</p> : null}
      <p className="mt-1 text-xs text-muted-foreground">
        {t("audit.agentVersion")}: {event.agent_version}
      </p>
    </li>
  );
}

export function AuditList({ reviewId, position }: { reviewId: string; position: number }) {
  const { t } = useTranslation();
  const audit = useQuery({
    queryKey: auditKey(reviewId, position),
    queryFn: () => api.reviews.audit(reviewId, position),
  });
  const events = [...(audit.data?.events ?? [])].sort((a, b) => b.event_id - a.event_id);
  return (
    <section data-testid="audit-list">
      <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {t("audit.title")}
      </h3>
      {audit.isError ? (
        <p role="alert" className="text-sm">
          {t("audit.loadError")}
        </p>
      ) : audit.isLoading ? (
        <p className="text-sm text-muted-foreground">{t("audit.loading")}</p>
      ) : events.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("audit.empty")}</p>
      ) : (
        <ol className="space-y-2">
          {events.map((event) => (
            <Entry key={event.event_id} event={event} />
          ))}
        </ol>
      )}
    </section>
  );
}

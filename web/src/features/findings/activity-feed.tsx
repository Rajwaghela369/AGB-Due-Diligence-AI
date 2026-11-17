// Collapsible live step feed. It is derived from status changes in `review.findings`, which
// already merge the stream into stored data, so it opens no stream of its own. The first
// snapshot is the baseline: a reload shows an empty feed, not a replay.
import { ChevronDown, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { ClauseStatus } from "../../api/aliases";
import { clauseTitle } from "./helpers";
import type { ReviewFinding } from "../review/use-review";

interface Entry {
  key: number;
  position: number;
  title: string;
  status: ClauseStatus;
}

interface Tracker {
  seen: Map<number, ClauseStatus> | null;
  log: Entry[];
}

export function ActivityFeed({ findings }: { findings: ReviewFinding[] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [tracker, setTracker] = useState<Tracker>({ seen: null, log: [] });

  // Adjusting state while rendering is React's documented way to derive from changing props.
  if (findings.length > 0) {
    const next = new Map(findings.map((f) => [f.position, f.status]));
    if (tracker.seen === null) {
      setTracker({ seen: next, log: [] });
    } else {
      const added: Entry[] = [];
      for (const f of findings) {
        if (tracker.seen.get(f.position) !== f.status) {
          added.push({
            key: tracker.log.length + added.length,
            position: f.position,
            title: clauseTitle(f),
            status: f.status,
          });
        }
      }
      if (added.length > 0) setTracker({ seen: next, log: [...tracker.log, ...added] });
    }
  }

  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <section className="rounded-lg border border-border bg-surface">
      <h3 className="text-sm font-medium">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 p-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Chevron className="size-4" aria-hidden />
          {t("findings.activity")}
        </button>
      </h3>
      {open && (
        <ol aria-live="polite" className="max-h-48 space-y-1 overflow-y-auto px-3 pb-3 text-xs">
          {tracker.log.length === 0 && (
            <li className="text-muted-foreground">{t("findings.activityEmpty")}</li>
          )}
          {[...tracker.log].reverse().map((entry) => (
            <li key={entry.key} className="text-muted-foreground">
              {entry.title} — {t(`findings.step.${entry.status}`)}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

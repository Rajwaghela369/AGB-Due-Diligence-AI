// The decision controls in the drawer: Akzeptieren (A), Überschreiben (O), Verwerfen (D), one
// optional note, and for an override the verdict + § picker. Hotkeys and card buttons reach
// this panel through the `decisionRequest` in the review UI store, so the note typed here
// goes with every route to a decision. Errors from the API are shown inline as they are.
import { Ban, CircleCheck, PencilLine } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import type { DecisionIn } from "../../api/types.gen";
import { cn } from "../../lib/utils";
import { useStatute } from "../findings/use-statute";
import { useReviewUi, type DecisionAction } from "../review/store";
import type { ReviewFinding } from "../review/use-review";
import { StatusBadge } from "./status-badge";
import { isDecidable } from "./status";
import { useDecide } from "./use-decide";

type Verdict = NonNullable<DecisionIn["corrected_verdict"]>;
const VERDICTS: Verdict[] = ["valid", "potentially_void", "uncertain"];
const NOTE_MAX = 2000;

const buttonClass = cn(
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm",
  "hover:bg-muted disabled:opacity-60",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
);

function BasisOption(props: {
  chunkId: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  const statute = useStatute(props.chunkId);
  return (
    <label className="flex items-start gap-2 text-sm">
      <input
        type="checkbox"
        className="mt-1 accent-accent"
        checked={props.checked}
        onChange={(event) => props.onChange(event.target.checked)}
      />
      {statute.data?.citation ?? props.chunkId}
    </label>
  );
}

function OverrideForm(props: {
  finding: ReviewFinding;
  busy: boolean;
  onSubmit: (verdict: Verdict, basis: string[]) => void;
  onCancel: () => void;
}) {
  const { finding } = props;
  const { t } = useTranslation();
  const modelVerdict = finding.verdict?.verdict;
  const suggestions = [
    ...new Set([...(finding.verdict?.legal_basis ?? []), ...(finding.verdict?.candidates ?? [])]),
  ];
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const [basis, setBasis] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);

  function submit() {
    if (verdict === "") return setProblem(t("decision.override.needVerdict"));
    if (verdict === modelVerdict && basis.length === 0)
      return setProblem(t("decision.override.needChange"));
    setProblem(null);
    props.onSubmit(verdict, basis);
  }

  return (
    <form
      data-testid="override-form"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
      onKeyDown={(event) => {
        // Esc closes the form first; the drawer's own Esc handler then leaves it open.
        if (event.key === "Escape") {
          event.preventDefault();
          props.onCancel();
        }
      }}
      className="space-y-3 rounded-md border border-border p-3"
    >
      <fieldset>
        <legend className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {t("decision.override.verdict")}
        </legend>
        <div className="space-y-1">
          {VERDICTS.map((value, index) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={`verdict-${finding.position}`}
                className="accent-accent"
                autoFocus={index === 0}
                checked={verdict === value}
                onChange={() => setVerdict(value)}
              />
              {t(`decision.verdict.${value}`)}
              {value === modelVerdict && (
                <span className="text-xs text-muted-foreground">
                  ({t("decision.override.model")})
                </span>
              )}
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {t("decision.override.basis")}
        </legend>
        {suggestions.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("decision.override.noBasis")}</p>
        ) : (
          <div className="space-y-1">
            {suggestions.map((chunkId) => (
              <BasisOption
                key={chunkId}
                chunkId={chunkId}
                checked={basis.includes(chunkId)}
                onChange={(checked) =>
                  setBasis((current) =>
                    checked ? [...current, chunkId] : current.filter((id) => id !== chunkId),
                  )
                }
              />
            ))}
          </div>
        )}
        <p className="mt-1 text-xs text-muted-foreground">{t("decision.override.basisHint")}</p>
      </fieldset>
      {problem && (
        <p role="alert" className="text-sm">
          {problem}
        </p>
      )}
      <div className="flex gap-2">
        <button type="submit" disabled={props.busy} className={buttonClass}>
          {t("decision.override.confirm")}
        </button>
        <button type="button" onClick={props.onCancel} className={buttonClass}>
          {t("decision.override.cancel")}
        </button>
      </div>
    </form>
  );
}

/** What is currently decided, in words: „Überschrieben: Wirksam · § 309 Nr. 7 BGB“. */
function CurrentDecision({ finding }: { finding: ReviewFinding }) {
  const { t } = useTranslation();
  const decision = finding.decision;
  if (!decision) return null;
  return (
    <p className="text-sm" data-testid="current-decision">
      {decision.action === "override" && decision.corrected_verdict
        ? t("decision.current.override", {
            verdict: t(`decision.verdict.${decision.corrected_verdict}`),
          })
        : t(`decision.current.${decision.action}`)}
      {decision.note ? <span className="text-muted-foreground"> — {decision.note}</span> : null}
    </p>
  );
}

export function DecisionPanel({ reviewId, finding }: { reviewId: string; finding: ReviewFinding }) {
  const { t } = useTranslation();
  const noteId = useId();
  const { decide, pending, error, clearError } = useDecide(reviewId);
  const [note, setNote] = useState("");
  const [overriding, setOverriding] = useState(false);
  const request = useReviewUi((s) => s.decisionRequest);
  const consume = useReviewUi((s) => s.consumeDecision);
  const position = finding.position;
  const decidable = isDecidable(finding);

  async function run(input: DecisionIn) {
    const ok = await decide(position, { ...input, note: note.trim() });
    if (ok) {
      setNote("");
      setOverriding(false);
    }
  }

  function act(action: DecisionAction) {
    clearError();
    if (action === "override") setOverriding(true);
    else void run({ action });
  }

  const mine = request?.position === position ? request : null;
  useEffect(() => {
    if (!mine) return;
    consume(mine.nonce);
    // Synchronize an external hotkey/card request with the local override form.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- external request boundary
    if (mine.action === "override" && decidable) setOverriding(true);
    else if (decidable && !pending) void run({ action: mine.action });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mine?.nonce]);

  if (!decidable) return null;
  const actions: Array<{
    action: DecisionAction;
    label: string;
    key: string;
    icon: typeof Ban;
  }> = [
    { action: "accept", label: t("decision.accept"), key: "A", icon: CircleCheck },
    { action: "override", label: t("decision.override.open"), key: "O", icon: PencilLine },
    { action: "dismiss", label: t("decision.dismiss"), key: "D", icon: Ban },
  ];

  return (
    <section data-testid="decision-panel" className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {t("decision.title")}
        </h3>
        <StatusBadge finding={finding} />
      </div>
      {finding.status === "awaiting_decision" && (
        <p className="text-sm">{t("decision.awaitingHint")}</p>
      )}
      <CurrentDecision finding={finding} />
      <div className="flex flex-wrap gap-2" role="group" aria-label={t("decision.title")}>
        {actions.map(({ action, label, key, icon: Icon }) => (
          <button
            key={action}
            type="button"
            disabled={pending}
            aria-keyshortcuts={key}
            aria-pressed={
              action === "override"
                ? overriding || finding.decision?.action === "override"
                : finding.decision?.action === action
            }
            onClick={() => act(action)}
            className={buttonClass}
          >
            <Icon className="size-4" aria-hidden />
            {label}
            <kbd className="rounded border border-border px-1 font-sans text-xs">{key}</kbd>
          </button>
        ))}
      </div>
      {overriding && (
        <OverrideForm
          finding={finding}
          busy={pending}
          onCancel={() => setOverriding(false)}
          onSubmit={(corrected_verdict, corrected_basis) =>
            void run({ action: "override", corrected_verdict, corrected_basis })
          }
        />
      )}
      <div>
        <label htmlFor={noteId} className="mb-1 block text-sm text-muted-foreground">
          {t("decision.note")}
        </label>
        <textarea
          id={noteId}
          value={note}
          maxLength={NOTE_MAX}
          rows={2}
          onChange={(event) => setNote(event.target.value)}
          className="w-full rounded-md border border-border bg-surface p-2 text-sm"
        />
      </div>
      {error && (
        <p role="alert" data-testid="decision-error" className="text-sm text-risk">
          {error}
        </p>
      )}
    </section>
  );
}

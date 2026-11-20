// 👍 / 👎 for one finding, used on the card and in the drawer (Phase 6 task 6.F). A 👍 posts
// at once; a 👎 opens reason chips and posts when sent. The vote can be changed at any time,
// and the stored vote (`finding.feedback`) is what the buttons show after a reload.
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";

import type { FeedbackIn } from "../../api/types.gen";
import { cn } from "../../lib/utils";
import type { ReviewFinding } from "../review/use-review";
import { parseBasis } from "./helpers";
import { useFeedback } from "./use-feedback";

type Reason = NonNullable<FeedbackIn["reasons"]>[number];
type Verdict = NonNullable<FeedbackIn["corrected_verdict"]>;

const REASONS: Reason[] = ["verdict_wrong", "basis_wrong", "reasoning_unclear", "clause_cut"];
const VERDICTS: Verdict[] = ["valid", "potentially_void", "uncertain"];

export function FeedbackControl(props: {
  reviewId: string;
  finding: ReviewFinding;
  compact?: boolean;
}) {
  const { reviewId, finding, compact = false } = props;
  const { t } = useTranslation();
  const stored = finding.feedback ?? null;
  const mutation = useFeedback(reviewId);
  const [open, setOpen] = useState(false);
  const formId = useId();

  const [reasons, setReasons] = useState<Reason[]>([]);
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const [basis, setBasis] = useState("");
  const [comment, setComment] = useState("");

  const base = { review_id: reviewId, position: finding.position };
  const iconSize = compact ? "size-3.5" : "size-4";

  function openForm() {
    // Reopening a stored 👎 starts from what was sent, so changing it is an edit.
    const down = stored?.vote === "down" ? stored : null;
    setReasons(down?.reasons ?? []);
    setVerdict(down?.corrected_verdict ?? "");
    setBasis((down?.corrected_basis ?? []).join(", "));
    setComment(down?.comment ?? "");
    setOpen(true);
  }

  function voteUp() {
    setOpen(false);
    mutation.mutate({ ...base, vote: "up" });
  }

  function toggleReason(reason: Reason) {
    setReasons((current) =>
      current.includes(reason) ? current.filter((r) => r !== reason) : [...current, reason],
    );
  }

  const needsVerdict = reasons.includes("verdict_wrong") && verdict === "";
  const needsBasis = reasons.includes("basis_wrong") && parseBasis(basis).length === 0;

  function send() {
    const input: FeedbackIn = { ...base, vote: "down", reasons };
    if (reasons.includes("verdict_wrong") && verdict !== "") input.corrected_verdict = verdict;
    if (reasons.includes("basis_wrong")) input.corrected_basis = parseBasis(basis);
    if (comment.trim() !== "") input.comment = comment.trim();
    setOpen(false);
    mutation.mutate(input);
  }

  const votedUp = stored?.vote === "up";
  const votedDown = stored?.vote === "down" || open;

  return (
    <div className="text-sm" data-testid={`feedback-${finding.position}`}>
      <div className="flex items-center gap-1">
        {!compact && <span className="mr-2 text-muted-foreground">{t("feedback.prompt")}</span>}
        <button
          type="button"
          aria-pressed={votedUp}
          aria-label={t("feedback.up")}
          title={t("feedback.up")}
          onClick={voteUp}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border border-transparent p-1.5",
            "hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            votedUp && "border-border bg-muted",
          )}
        >
          <ThumbsUp className={iconSize} aria-hidden />
          {votedUp && <span className="text-xs">{t("feedback.sentUp")}</span>}
        </button>
        <button
          type="button"
          aria-pressed={votedDown}
          aria-expanded={open}
          aria-controls={open ? formId : undefined}
          aria-label={t("feedback.down")}
          title={t("feedback.down")}
          onClick={() => (open ? setOpen(false) : openForm())}
          className={cn(
            "inline-flex items-center gap-1 rounded-md border border-transparent p-1.5",
            "hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
            votedDown && "border-border bg-muted",
          )}
        >
          <ThumbsDown className={iconSize} aria-hidden />
          {stored?.vote === "down" && <span className="text-xs">{t("feedback.sentDown")}</span>}
        </button>
        {mutation.isError && (
          <span role="alert" className="ml-2 text-xs text-risk">
            {t("feedback.error")}
          </span>
        )}
      </div>

      {open && (
        <form
          id={formId}
          className="mt-2 space-y-3 rounded-md border border-border bg-background p-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (!needsVerdict && !needsBasis) send();
          }}
        >
          <fieldset>
            <legend className="mb-1 text-xs text-muted-foreground">{t("feedback.reasons")}</legend>
            <div className="flex flex-wrap gap-2">
              {REASONS.map((reason) => (
                <label
                  key={reason}
                  className={cn(
                    "cursor-pointer rounded-full border border-border px-3 py-1 text-xs",
                    "has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-ring",
                    reasons.includes(reason) && "bg-muted font-medium",
                  )}
                >
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={reasons.includes(reason)}
                    onChange={() => toggleReason(reason)}
                  />
                  {t(`feedback.reason.${reason}`)}
                </label>
              ))}
            </div>
          </fieldset>

          {reasons.includes("verdict_wrong") && (
            <label className="block text-xs">
              <span className="mb-1 block text-muted-foreground">
                {t("feedback.correctVerdict")}
              </span>
              <select
                value={verdict}
                onChange={(event) => setVerdict(event.target.value as Verdict | "")}
                className="w-full rounded-md border border-border bg-surface p-2 text-sm"
              >
                <option value="">{t("feedback.chooseVerdict")}</option>
                {VERDICTS.map((v) => (
                  <option key={v} value={v}>
                    {t(`feedback.verdict.${v}`)}
                  </option>
                ))}
              </select>
            </label>
          )}

          {reasons.includes("basis_wrong") && (
            <label className="block text-xs">
              <span className="mb-1 block text-muted-foreground">{t("feedback.correctBasis")}</span>
              <input
                type="text"
                value={basis}
                onChange={(event) => setBasis(event.target.value)}
                placeholder={t("feedback.basisPlaceholder")}
                className="w-full rounded-md border border-border bg-surface p-2 text-sm"
              />
            </label>
          )}

          <label className="block text-xs">
            <span className="mb-1 block text-muted-foreground">{t("feedback.comment")}</span>
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              rows={2}
              className="w-full rounded-md border border-border bg-surface p-2 text-sm"
            />
          </label>

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={needsVerdict || needsBasis}
              className="rounded-md bg-foreground px-3 py-1.5 text-xs text-background disabled:opacity-50"
            >
              {t("feedback.send")}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md px-3 py-1.5 text-xs hover:bg-muted"
            >
              {t("feedback.cancel")}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

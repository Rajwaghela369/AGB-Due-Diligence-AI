// The rewrite chat in the finding drawer (Phase 8, 8.C): „Formuliere 6.1 rechtskonform um“.
// Collapsed by default, since each question is a paid model call and most findings need
// none. The answer streams in as plain text; once it is final, the suggested wording is
// shown as a word diff against the clause, with the disclaimer under every answer.
import { ChevronDown, ChevronRight, Copy, Loader2, RotateCcw, Send, Square } from "lucide-react";
import { useId, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import type { ReviewFinding } from "../review/use-review";
import { DiffView } from "./diff-view";
import { MAX_MESSAGE_LENGTH, useRewriteChat } from "./use-rewrite-chat";
import type { AssistantTurn, Turn } from "./use-rewrite-chat";

const BUTTON =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-sm hover:bg-muted disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Answer({
  turn,
  onRetry,
  busy,
}: {
  turn: AssistantTurn;
  onRetry: () => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const result = turn.result;
  const suggestion = result?.suggestion;

  const copy = async () => {
    if (!suggestion) return;
    try {
      await navigator.clipboard.writeText(suggestion);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="space-y-2" data-testid="chat-answer">
      <p className="text-xs font-semibold text-muted-foreground">{t("chat.assistant")}</p>
      {turn.status === "streaming" && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" aria-hidden />
          {t("chat.streaming")}
        </p>
      )}
      {turn.text && (
        <p className="text-sm leading-relaxed whitespace-pre-line" data-testid="chat-answer-text">
          {turn.text}
        </p>
      )}
      {result && suggestion && result.diff && result.diff.length > 0 && (
        <section className="space-y-1.5 rounded-md border border-border p-2">
          <div className="flex items-center gap-2">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              {t("chat.suggestion")}
            </h4>
            <button
              type="button"
              onClick={() => void copy()}
              className="ml-auto inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <Copy className="size-3.5" aria-hidden />
              {copied ? t("chat.copied") : t("chat.copy")}
            </button>
          </div>
          <DiffView diff={result.diff} />
        </section>
      )}
      {result?.truncated && <p className="text-xs text-muted-foreground">{t("chat.truncated")}</p>}
      {result && (
        <p className="rounded bg-disclaimer px-2 py-1 text-xs" data-testid="chat-disclaimer">
          {t("chat.disclaimer")}
        </p>
      )}
      {turn.status === "stopped" && (
        <p className="text-xs text-muted-foreground">{t("chat.stopped")}</p>
      )}
      {turn.status === "error" && turn.error && (
        <div className="space-y-1.5">
          <p role="alert" className="text-sm">
            {t(`chat.errors.${turn.error.code}`, {
              defaultValue: turn.error.message || t("chat.errors.chat_failed"),
            })}
          </p>
        </div>
      )}
      {(turn.status === "error" || turn.status === "stopped") && (
        <button type="button" onClick={onRetry} disabled={busy} className={BUTTON}>
          <RotateCcw className="size-4" aria-hidden />
          {t("chat.retry")}
        </button>
      )}
    </div>
  );
}

function TurnView({ turn, onRetry, busy }: { turn: Turn; onRetry: () => void; busy: boolean }) {
  const { t } = useTranslation();
  if (turn.role === "assistant") return <Answer turn={turn} onRetry={onRetry} busy={busy} />;
  return (
    <div className="space-y-1" data-testid="chat-question">
      <p className="text-xs font-semibold text-muted-foreground">{t("chat.you")}</p>
      <p className="rounded-md bg-muted px-2 py-1.5 text-sm whitespace-pre-line">{turn.content}</p>
    </div>
  );
}

/** The per-finding rewrite chat; only mount it for a finding with a verdict. */
export function RewriteChat({ reviewId, finding }: { reviewId: string; finding: ReviewFinding }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const { turns, busy, send, stop } = useRewriteChat(reviewId, finding.position);
  const panelId = useId();
  const inputId = useId();
  const clause = finding.clause.number ?? finding.clause.heading ?? String(finding.position + 1);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    if (busy || !draft.trim()) return;
    void send(draft);
    setDraft("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  };
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <section className="rounded-md border border-border" data-testid="rewrite-chat">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-2 p-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Chevron className="size-4 shrink-0" aria-hidden />
          {t("chat.title")}
        </button>
      </h3>
      {open && (
        <div id={panelId} className="space-y-3 border-t border-border p-3">
          <p className="text-sm text-muted-foreground">{t("chat.intro")}</p>
          {turns.length > 0 && (
            <div role="log" aria-live="polite" aria-label={t("chat.log")} className="space-y-3">
              {turns.map((turn) => (
                <TurnView
                  key={turn.id}
                  turn={turn}
                  busy={busy}
                  onRetry={() => turn.role === "assistant" && void send(turn.question)}
                />
              ))}
            </div>
          )}
          <form onSubmit={submit} className="space-y-2">
            <label htmlFor={inputId} className="sr-only">
              {t("chat.inputLabel")}
            </label>
            <textarea
              id={inputId}
              value={draft}
              maxLength={MAX_MESSAGE_LENGTH}
              rows={2}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={t("chat.placeholder")}
              className="w-full resize-y rounded-md border border-border bg-background p-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <div className="flex flex-wrap gap-2">
              {turns.length === 0 && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void send(t("chat.quickPrompt", { clause }))}
                  className={BUTTON}
                >
                  {t("chat.quickPrompt", { clause })}
                </button>
              )}
              {busy ? (
                <button type="button" onClick={stop} className={`${BUTTON} ml-auto`}>
                  <Square className="size-4" aria-hidden />
                  {t("chat.stop")}
                </button>
              ) : (
                <button type="submit" disabled={!draft.trim()} className={`${BUTTON} ml-auto`}>
                  <Send className="size-4" aria-hidden />
                  {t("chat.send")}
                </button>
              )}
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

// The state of one finding's rewrite chat (Phase 8, 8.C). The server keeps no chat: the
// turns live here, and each question sends the finished ones back as `history`. One question
// is one streamed model call (docs/Events.md, "Rewrite chat"): the deltas render as they
// arrive, then `chat_result` replaces them with the final, capped answer and its suggestion.
// A dropped stream or a RUN_ERROR leaves no result; the lawyer can ask again.
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../../api/client";
import type { ChatIn, ChatResult } from "../../api/types.gen";
import { useReviewer } from "../decisions/reviewer";
import { readSse } from "./sse";

/** ChatIn allows at most 20 history messages; the server uses only its newest ones. */
export const MAX_HISTORY = 20;
export const MAX_MESSAGE_LENGTH = 2000;

export interface ChatError {
  /** i18n key suffix under `chat.errors.`, e.g. "chat_failed", "daily_budget". */
  code: string;
  /** The server's German message, used when the code has no translation. */
  message: string;
}

export interface UserTurn {
  id: number;
  role: "user";
  content: string;
}

export interface AssistantTurn {
  id: number;
  role: "assistant";
  /** The question this answers, so a failed answer can be asked again. */
  question: string;
  status: "streaming" | "done" | "error" | "stopped";
  /** The streamed text so far; replaced by `result.text` once the result arrives. */
  text: string;
  result: ChatResult | null;
  error: ChatError | null;
}

export type Turn = UserTurn | AssistantTurn;

interface Payload {
  name?: string;
  value?: unknown;
  delta?: string;
  code?: string;
  message?: string;
}

/** The finished exchanges as `ChatIn.history`: failed or stopped answers and their question
 * are left out, since the model never gave them. */
export function historyOf(turns: Turn[]): NonNullable<ChatIn["history"]> {
  const history: { role: "user" | "assistant"; content: string }[] = [];
  turns.forEach((turn, index) => {
    if (turn.role !== "assistant" || turn.status !== "done" || !turn.result) return;
    const question = turns[index - 1];
    if (question?.role === "user") history.push({ role: "user", content: question.content });
    history.push({ role: "assistant", content: turn.result.text });
  });
  return history.slice(-MAX_HISTORY) as NonNullable<ChatIn["history"]>;
}

export function useRewriteChat(reviewId: string, position: number) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const turnsRef = useRef<Turn[]>([]);
  const nextId = useRef(0);
  const controller = useRef<AbortController | null>(null);

  const update = useCallback((change: (turns: Turn[]) => Turn[]) => {
    turnsRef.current = change(turnsRef.current);
    setTurns(turnsRef.current);
  }, []);

  // Leaving the finding ends its stream; nothing would show the answer.
  useEffect(() => () => controller.current?.abort(), []);

  const busy = turns.some((turn) => turn.role === "assistant" && turn.status === "streaming");

  const send = useCallback(
    async (raw: string) => {
      const question = raw.trim().slice(0, MAX_MESSAGE_LENGTH);
      if (!question || controller.current) return;
      const history = historyOf(turnsRef.current);
      const answerId = nextId.current + 1;
      nextId.current += 2;
      update((current) => [
        ...current,
        { id: answerId - 1, role: "user", content: question },
        {
          id: answerId,
          role: "assistant",
          question,
          status: "streaming",
          text: "",
          result: null,
          error: null,
        },
      ]);
      const patch = (change: Partial<AssistantTurn>) =>
        update((current) =>
          current.map((turn) =>
            turn.id === answerId && turn.role === "assistant" ? { ...turn, ...change } : turn,
          ),
        );
      const abort = new AbortController();
      controller.current = abort;
      let text = "";
      let result: ChatResult | null = null;
      let error: ChatError | null = null;
      let finished = false;
      try {
        const response = await api.reviews.chat(
          reviewId,
          position,
          { message: question, history },
          { reviewer: useReviewer.getState().name || null, signal: abort.signal },
        );
        if (!response.body) throw new Error("no body");
        for await (const message of readSse(response.body, abort.signal)) {
          const payload = JSON.parse(message.data) as Payload;
          if (message.event === "TEXT_MESSAGE_CONTENT" && payload.delta) {
            text += payload.delta;
            patch({ text });
          } else if (message.event === "CUSTOM" && payload.name === "chat_result") {
            result = payload.value as ChatResult;
          } else if (message.event === "RUN_FINISHED") {
            finished = true;
          } else if (message.event === "RUN_ERROR") {
            // An error invalidates any result received before it.
            result = null;
            finished = false;
            error = { code: payload.code ?? "chat_failed", message: payload.message ?? "" };
          }
        }
        if (result && finished && !error) patch({ status: "done", text: result.text, result });
        else patch({ status: "error", error: error ?? { code: "interrupted", message: "" } });
      } catch (caught) {
        if (abort.signal.aborted) patch({ status: "stopped" });
        else if (caught instanceof ApiError)
          patch({ status: "error", error: { code: caught.body.code, message: caught.message } });
        else patch({ status: "error", error: { code: "interrupted", message: "" } });
      } finally {
        if (controller.current === abort) controller.current = null;
      }
    },
    [reviewId, position, update],
  );

  const stop = useCallback(() => controller.current?.abort(), []);

  return { turns, busy, send, stop };
}

/** Resumable AG-UI review events, kept independent from any component lifecycle. */

import { applyPatch, type Operation } from "fast-json-patch";
import { useEffect, useState } from "react";

import type { ClauseVerdict, Review, Status1, Type } from "./types.gen";

export interface ReviewClauseState {
  status: Status1;
  error: string | null;
  verdict: ClauseVerdict | null;
}

export interface ReviewState {
  review: Review;
  clauses: Record<string, ReviewClauseState>;
}

interface ReviewEvent {
  type: Type;
  snapshot?: ReviewState;
  delta?: Operation[];
  [key: string]: unknown;
}

export interface ReviewStreamUpdate {
  state: ReviewState | null;
  event: ReviewEvent;
  lastEventId: string | null;
}

export interface ReviewStreamOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
  signal?: AbortSignal;
  initialBackoffMs?: number;
  maxBackoffMs?: number;
  sleep?: (milliseconds: number, signal: AbortSignal) => Promise<void>;
}

export interface ReviewStream {
  close(): void;
  done: Promise<void>;
}

const configuredBaseUrl = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export function connectReviewStream(
  reviewId: string,
  onUpdate: (update: ReviewStreamUpdate) => void,
  options: ReviewStreamOptions = {},
): ReviewStream {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abortFromOutside = () => controller.abort(externalSignal?.reason);
  externalSignal?.addEventListener("abort", abortFromOutside, { once: true });

  const done = consumeReviewStream(reviewId, onUpdate, options, controller.signal).finally(() => {
    externalSignal?.removeEventListener("abort", abortFromOutside);
  });
  return { close: () => controller.abort(), done };
}

async function consumeReviewStream(
  reviewId: string,
  onUpdate: (update: ReviewStreamUpdate) => void,
  options: ReviewStreamOptions,
  signal: AbortSignal,
): Promise<void> {
  const fetchImplementation = options.fetch;
  const baseUrl = (options.baseUrl ?? configuredBaseUrl).replace(/\/$/, "");
  const sleep = options.sleep ?? abortableSleep;
  const initialBackoff = options.initialBackoffMs ?? 250;
  const maxBackoff = options.maxBackoffMs ?? 5_000;
  let backoff = initialBackoff;
  let lastEventId: string | null = null;
  let state: ReviewState | null = null;

  while (!signal.aborted) {
    try {
      const headers = new Headers({ Accept: "text/event-stream" });
      if (lastEventId !== null) headers.set("Last-Event-ID", lastEventId);
      const response = await (fetchImplementation ?? globalThis.fetch)(
        `${baseUrl}/api/v1/reviews/${encodeURIComponent(reviewId)}/events`,
        { headers, signal },
      );
      if (!response.ok || response.body === null) {
        throw new Error(`Review stream failed with HTTP ${response.status}`);
      }
      backoff = initialBackoff;

      for await (const message of parseEventStream(response.body, signal)) {
        if (message.id !== null) lastEventId = message.id;
        if (message.data.length === 0) continue;
        const parsed = JSON.parse(message.data) as ReviewEvent;
        const event = message.event ? { ...parsed, type: message.event as Type } : parsed;
        if (event.type === "STATE_SNAPSHOT" && event.snapshot !== undefined) {
          state = structuredClone(event.snapshot);
        } else if (event.type === "STATE_DELTA" && event.delta !== undefined && state !== null) {
          state = applyPatch(structuredClone(state), event.delta, true, false).newDocument;
        }
        onUpdate({ state, event, lastEventId });
        if (event.type === "RUN_FINISHED" || event.type === "RUN_ERROR") return;
      }
    } catch (error) {
      if (signal.aborted) return;
      if (error instanceof SyntaxError) throw error;
    }

    await sleep(backoff, signal).catch(() => undefined);
    backoff = Math.min(backoff * 2, maxBackoff);
  }
}

interface SseMessage {
  id: string | null;
  event: string | null;
  data: string;
}

async function* parseEventStream(
  stream: ReadableStream<Uint8Array>,
  signal: AbortSignal,
): AsyncGenerator<SseMessage> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary = findSseBoundary(buffer);
      while (boundary !== null) {
        const block = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary.length);
        const message = parseSseBlock(block);
        if (message !== null) yield message;
        boundary = findSseBoundary(buffer);
      }
      if (done) break;
    }
    if (buffer.trim()) {
      const message = parseSseBlock(buffer);
      if (message !== null) yield message;
    }
  } finally {
    reader.releaseLock();
  }
}

function parseSseBlock(block: string): SseMessage | null {
  let id: string | null = null;
  let event: string | null = null;
  const data: string[] = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    const rawValue = separator < 0 ? "" : line.slice(separator + 1);
    const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
    if (field === "id") id = value;
    else if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (id === null && event === null && data.length === 0) return null;
  return { id, event, data: data.join("\n") };
}

function findSseBoundary(buffer: string): { index: number; length: number } | null {
  const match = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
  return match === null ? null : { index: match.index, length: match[0].length };
}

function abortableSleep(milliseconds: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(signal.reason);
      },
      { once: true },
    );
  });
}

export function useReviewStream(reviewId: string | undefined) {
  const [current, setCurrent] = useState<{ reviewId: string; state: ReviewState | null } | null>(
    null,
  );
  const [failure, setFailure] = useState<{ reviewId: string; error: Error } | null>(null);

  useEffect(() => {
    if (reviewId === undefined) return;
    const stream = connectReviewStream(reviewId, ({ state }) => setCurrent({ reviewId, state }));
    void stream.done.catch((reason: unknown) => {
      const error = reason instanceof Error ? reason : new Error(String(reason));
      setFailure({ reviewId, error });
    });
    return stream.close;
  }, [reviewId]);

  return {
    state: current !== null && current.reviewId === reviewId ? current.state : null,
    error: failure !== null && failure.reviewId === reviewId ? failure.error : null,
  };
}

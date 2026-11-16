import { describe, expect, it, vi } from "vitest";

import { connectReviewStream, type ReviewState, type ReviewStreamUpdate } from "./events";

const queuedState: ReviewState = {
  review: {
    review_id: "review-1",
    document_id: "document-1",
    agent_version: "agent-1",
    scope: ["liability"],
    status: "running",
    counts: { total: 1 },
  },
  clauses: {
    "0": { status: "queued", error: null, verdict: null },
  },
};

describe("review event stream", () => {
  it("resumes a dropped stream without advancing the id for a reconnect snapshot", async () => {
    const first = sseResponse([
      sse("STATE_SNAPSHOT", { type: "STATE_SNAPSHOT", snapshot: queuedState }, "1"),
      sse(
        "STATE_DELTA",
        {
          type: "STATE_DELTA",
          delta: [
            {
              op: "replace",
              path: "/clauses/0",
              value: { status: "checking", error: null, verdict: null },
            },
          ],
        },
        "2",
      ),
    ]);
    const reconnectSnapshot: ReviewState = structuredClone(queuedState);
    reconnectSnapshot.clauses["0"] = { status: "checking", error: null, verdict: null };
    const verdict = {
      content_hash: "hash",
      agent_version: "agent-1",
      verdict: "potentially_void" as const,
      confidence: 0.92,
    };
    const second = sseResponse([
      sse("STATE_SNAPSHOT", { type: "STATE_SNAPSHOT", snapshot: reconnectSnapshot }),
      sse(
        "STATE_DELTA",
        {
          type: "STATE_DELTA",
          delta: [
            {
              op: "replace",
              path: "/clauses/0",
              value: { status: "done", error: null, verdict },
            },
          ],
        },
        "3",
      ),
      sse("RUN_FINISHED", { type: "RUN_FINISHED", result: { counts: { done: 1 } } }, "4"),
    ]);
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    const updates: ReviewStreamUpdate[] = [];

    const stream = connectReviewStream("review-1", (update) => updates.push(update), {
      baseUrl: "https://api.example",
      fetch: fetchMock,
      sleep: async () => undefined,
    });
    await stream.done;

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const reconnectInit = fetchMock.mock.calls[1]?.[1];
    expect(new Headers(reconnectInit?.headers).get("Last-Event-ID")).toBe("2");
    const snapshotUpdate = updates.find(
      (update, index) => index > 1 && update.event.type === "STATE_SNAPSHOT",
    );
    expect(snapshotUpdate?.lastEventId).toBe("2");
    expect(updates.at(-1)?.state?.clauses["0"]).toEqual({
      status: "done",
      error: null,
      verdict,
    });
    expect(updates.at(-1)?.event.type).toBe("RUN_FINISHED");
  });
});

function sse(event: string, data: object, id?: string): string {
  return `${id === undefined ? "" : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function sseResponse(messages: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const message of messages) controller.enqueue(encoder.encode(message));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

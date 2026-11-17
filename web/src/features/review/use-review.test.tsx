import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { StrictMode, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DocumentOut, FindingsOut, Review } from "../../api/types.gen";
import { server } from "../../test/server";
import { clauseTitle } from "./clause-title";
import { useReviewUi } from "./store";
import { useReview } from "./use-review";

const REVIEW = {
  review_id: "r1",
  document_id: "d1",
  agent_version: "v1",
  scope: ["liability"],
  status: "running",
  counts: { total: 1, done: 0 },
} as Review;

const DOCUMENT = {
  document: { document_id: "d1", filename: "AGB.pdf", source_type: "pdf" },
  clauses: [],
} as unknown as DocumentOut;

function sse(events: Array<{ id: number; type: string; data: unknown }>): Response {
  const body = events
    .map(
      (e) =>
        `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify({ type: e.type, ...(e.data as object) })}\n\n`,
    )
    .join("");
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

let findingRequests = 0;
let streamOpens = 0;

beforeEach(() => {
  findingRequests = 0;
  streamOpens = 0;
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
    real(
      typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
      init,
    ),
  );
  server.use(
    http.get("http://localhost/api/v1/reviews/r1/findings", () => {
      findingRequests += 1;
      return HttpResponse.json({ review: REVIEW, findings: [] } satisfies FindingsOut);
    }),
    http.get("http://localhost/api/v1/documents/d1", () => HttpResponse.json(DOCUMENT)),
    http.get("http://localhost/api/v1/reviews/r1/events", () => {
      streamOpens += 1;
      return sse([
        { id: 1, type: "STATE_SNAPSHOT", data: { snapshot: { review: REVIEW, clauses: {} } } },
        {
          id: 2,
          type: "CUSTOM",
          data: { name: "progress", value: { waiting_reason: "rate_limit", budget_left: 7 } },
        },
        { id: 3, type: "RUN_FINISHED", data: {} },
      ]);
    }),
  );
});

afterEach(() => {
  cleanup();
  useReviewUi.getState().reset();
});

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function strictWrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <StrictMode>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </StrictMode>
  );
}

describe("useReview", () => {
  it("exposes the document, the latest progress and a refresh that reopens the stream", async () => {
    const { result } = renderHook(() => useReview("r1"), { wrapper });
    await waitFor(() => expect(result.current.document).not.toBeNull());
    expect(result.current.document?.document.source_type).toBe("pdf");
    await waitFor(() =>
      expect(result.current.progress).toEqual({ waiting_reason: "rate_limit", budget_left: 7 }),
    );

    const requests = findingRequests;
    const opens = streamOpens;
    await act(() => result.current.refresh());
    await waitFor(() => expect(streamOpens).toBeGreaterThan(opens));
    expect(findingRequests).toBeGreaterThan(requests);
  });

  it("waits for stored findings and never streams a finished review", async () => {
    server.use(
      http.get("http://localhost/api/v1/reviews/r1/findings", () =>
        HttpResponse.json({
          review: { ...REVIEW, status: "finished" },
          findings: [
            {
              position: 0,
              status: "done",
              clause: {
                position: 0,
                number: "3.5",
                heading: "3 Lieferung",
                text: "3.5 Lieferung erfolgt.",
                content_hash: "hash",
              },
              verdict: null,
            },
          ],
        } as FindingsOut),
      ),
    );

    const { result } = renderHook(() => useReview("r1"), { wrapper: strictWrapper });
    await waitFor(() => expect(result.current.isFinal).toBe(true));
    await act(() => new Promise((resolve) => window.setTimeout(resolve, 10)));

    expect(streamOpens).toBe(0);
    // The stored heading is never rewritten; titles are formatted at render time.
    expect(result.current.findings[0]?.clause.heading).toBe("3 Lieferung");
  });

  it("opens one stream for a running review under Strict Mode", async () => {
    renderHook(() => useReview("r1"), { wrapper: strictWrapper });

    await waitFor(() => expect(streamOpens).toBe(1));
  });
});

describe("clauseTitle", () => {
  it.each([
    ["§ 6", "§ 6 Haftung", "§ 6 · Haftung"],
    ["6.1", "6 Lieferung", "6.1 · Lieferung"],
    ["(2)", "§ 6 Haftung", "(2) · Haftung"],
    ["a)", "6 Lieferung", "a) · Lieferung"],
  ])("formats %s without repeating the heading number", (number, heading, expected) => {
    expect(clauseTitle({ position: 0, number, heading, text: "Text", content_hash: "hash" })).toBe(
      expected,
    );
  });
});

describe("focusSearch", () => {
  it("bumps a counter each time the search is requested", () => {
    const before = useReviewUi.getState().focusSearch;
    useReviewUi.getState().requestSearchFocus();
    useReviewUi.getState().requestSearchFocus();
    expect(useReviewUi.getState().focusSearch).toBe(before + 2);
  });
});

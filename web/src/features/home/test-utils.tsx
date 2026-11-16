/* eslint-disable react-refresh/only-export-components -- test helpers, not a fast-refresh module */
/** Shared test harness: relative API paths go to a fixed origin so MSW can intercept them. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, vi } from "vitest";

import "../../i18n";

export const ORIGIN = "http://localhost";

/** Makes the module-level `api` client (empty base URL) reach MSW under jsdom. */
export function installOriginFetch() {
  beforeEach(() => {
    // Read at test time: MSW patches fetch in its own beforeAll.
    const original = globalThis.fetch;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      original(typeof input === "string" && input.startsWith("/") ? ORIGIN + input : input, init),
    );
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });
}

function LocationProbe() {
  const location = useLocation();
  return <p data-testid="location">{location.pathname}</p>;
}

export function renderPage(page: ReactElement, path = "/") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={path} element={page} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export function review(overrides: Record<string, unknown> = {}) {
  return {
    agent_version: "v1",
    document_id: "doc-1",
    review_id: "rev-1",
    scope: ["liability", "warranty"],
    status: "finished",
    counts: { total: 10, done: 10, failed: 0, potentially_void: 2, uncertain: 3, valid: 5 },
    ...overrides,
  };
}

export function documentOut(overrides: Record<string, unknown> = {}) {
  return {
    clauses: [],
    document: documentRecord(),
    latest_review: null,
    reused: false,
    ...overrides,
  };
}

export function documentRecord(overrides: Record<string, unknown> = {}) {
  return {
    document_id: "doc-1",
    filename: "vertrag.pdf",
    parser_version: "p1",
    source_type: "pdf",
    splitter_version: "s1",
    text_hash: "h",
    created_at: "2026-11-10T10:00:00Z",
    ...overrides,
  };
}

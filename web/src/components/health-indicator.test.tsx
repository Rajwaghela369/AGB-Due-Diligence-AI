import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createApiClient } from "../api/client";
import "../i18n";
import { server } from "../test/server";
import { HealthIndicator } from "./health-indicator";

describe("health indicator", () => {
  it("shows a reachable API", async () => {
    server.use(
      http.get("http://localhost/api/v1/health", () => HttpResponse.json({ status: "ok" })),
    );
    const client = createApiClient({ baseUrl: "http://localhost" });

    render(<HealthIndicator client={client} />, { wrapper: queryWrapper() });

    expect(await screen.findByText("API erreichbar")).toBeInTheDocument();
  });

  it("shows an unreachable API", async () => {
    server.use(
      http.get("http://localhost/api/v1/health", () =>
        HttpResponse.json({ code: "offline", message: "offline" }, { status: 503 }),
      ),
    );
    const client = createApiClient({ baseUrl: "http://localhost" });

    render(<HealthIndicator client={client} />, { wrapper: queryWrapper() });

    expect(await screen.findByText("API nicht erreichbar")).toBeInTheDocument();
  });
});

function queryWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

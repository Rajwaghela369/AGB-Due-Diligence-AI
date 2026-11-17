import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "./client";

describe("API client", () => {
  it("parses the backend ErrorBody", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ code: "not_found", message: "Prüfung fehlt" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const client = createApiClient({ baseUrl: "https://api.example", fetch: fetchMock });

    const request = client.reviews.get("review/1");

    await expect(request).rejects.toMatchObject({
      status: 404,
      body: { code: "not_found", message: "Prüfung fehlt" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example/api/v1/reviews/review%2F1",
      undefined,
    );
  });

  it("normalizes non-JSON errors to the same shape", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response("bad gateway", { status: 502, statusText: "Bad Gateway" }));
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await expect(client.health()).rejects.toMatchObject({
      body: { code: "http_error", message: "Bad Gateway" },
    });
  });

  it("sends the reviewer name percent-encoded, so an umlaut survives the Latin-1 header", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await client.reviews.decide("r1", 3, { action: "accept", note: "" }, "  Müller ");

    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/reviews/r1/clauses/3/decision");
    expect(init?.method).toBe("PUT");
    expect((init?.headers as Record<string, string>)["X-Reviewer"]).toBe("M%C3%BCller");
    expect(JSON.parse(init?.body as string)).toEqual({ action: "accept", note: "" });
  });

  it("sends no reviewer header without a name", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(() => Promise.resolve(Response.json({})));
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await client.reviews.decide("r1", 3, { action: "dismiss" }, "");
    await client.reviews.decide("r1", 3, { action: "dismiss" });

    for (const [, init] of fetchMock.mock.calls)
      expect(init?.headers as Record<string, string>).not.toHaveProperty("X-Reviewer");
  });

  it("reads the audit trail (optionally per finding) and the agreement", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(() =>
        Promise.resolve(
          new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
        ),
      );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await client.reviews.audit("r1");
    await client.reviews.audit("r1", 4);
    await client.metrics.agreement();
    await client.metrics.agreement("v 1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/reviews/r1/audit",
      "/api/v1/reviews/r1/audit?position=4",
      "/api/v1/metrics/agreement",
      "/api/v1/metrics/agreement?agent_version=v%201",
    ]);
  });
});

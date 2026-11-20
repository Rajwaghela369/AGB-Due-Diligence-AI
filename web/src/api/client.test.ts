import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "./client";

describe("API client", () => {
  it("attaches a bearer token to REST requests when configured", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(Response.json({}));
    const client = createApiClient({
      baseUrl: "https://api.example",
      fetch: fetchMock,
      getToken: async () => "neon-jwt",
    });

    await client.health();

    const init = fetchMock.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer neon-jwt");
  });

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

describe("Phase 8 client", () => {
  it("downloads an export with its filename and draft flag", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("a;b\r\n", {
        status: 200,
        headers: {
          "Content-Type": "text/csv; charset=utf-8",
          "Content-Disposition": "attachment; filename*=UTF-8''AGB-Check_M%C3%BCller_Entwurf.csv",
          "X-Export-Draft": "true",
        },
      }),
    );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    const download = await client.reviews.export("r1", "csv", { draft: true, reviewer: "Müller" });

    expect(download.filename).toBe("AGB-Check_Müller_Entwurf.csv");
    expect(download.draft).toBe(true);
    expect(await download.blob.text()).toBe("a;b\r\n");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/reviews/r1/export?format=csv&draft=true");
    expect((init?.headers as Record<string, string>)["X-Reviewer"]).toBe("M%C3%BCller");
  });

  it("keeps the blocking findings of a 409 export_blocked", async () => {
    const body = {
      code: "export_blocked",
      message: "Offene Feststellungen",
      reason: "undecided",
      blocking: [{ position: 2, number: "6.1", heading: null, verdict: "potentially_void" }],
    };
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response(JSON.stringify(body), { status: 409 }));
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await expect(client.reviews.export("r1", "docx")).rejects.toMatchObject({
      status: 409,
      body,
    });
  });

  it("opens the chat stream as a POST that accepts SSE", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("event: RUN_STARTED\ndata: {}\n\n", {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      }),
    );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    const response = await client.reviews.chat("r1", 4, { message: "Formuliere um" });

    expect(await response.text()).toContain("RUN_STARTED");
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/v1/reviews/r1/clauses/4/chat");
    expect(init?.method).toBe("POST");
    expect((init?.headers as Record<string, string>).Accept).toBe("text/event-stream");
    expect(JSON.parse(init?.body as string)).toEqual({ message: "Formuliere um" });
  });
});

describe("Phase 9 client", () => {
  it("deletes a document, optionally as a dry run, and reads the caller", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify({}), { headers: { "Content-Type": "application/json" } }),
        ),
      );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await client.documents.delete("doc/1", { dryRun: true });
    await client.documents.delete("doc/1");
    await client.me();

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method])).toEqual([
      ["/api/v1/documents/doc%2F1?dry_run=true", "DELETE"],
      ["/api/v1/documents/doc%2F1", "DELETE"],
      ["/api/v1/me", undefined],
    ]);
  });

  it("keeps the running reviews of a 409 deletion_blocked", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ code: "deletion_blocked", message: "läuft", review_ids: ["r1"] }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      );
    const client = createApiClient({ baseUrl: "", fetch: fetchMock });

    await expect(client.documents.delete("d")).rejects.toMatchObject({
      status: 409,
      body: { code: "deletion_blocked", review_ids: ["r1"] },
    });
  });
});

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
});

/** Typed access to the frozen Phase 5 endpoint surface and its uniform errors. */

import type {
  Decision,
  DecisionIn,
  DocumentList,
  DocumentOut,
  ErrorBody,
  FeedbackIn,
  FeedbackOut,
  Finding,
  FindingsOut,
  Health,
  PasteIn,
  Ready,
  ReviewIn,
  ReviewOut,
  StatuteOut,
} from "./types.gen";

export class ApiError extends Error {
  readonly status: number;
  readonly body: ErrorBody;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
}

const configuredBaseUrl = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export function createApiClient(options: ApiClientOptions = {}) {
  const baseUrl = (options.baseUrl ?? configuredBaseUrl).replace(/\/$/, "");
  const fetchImplementation = options.fetch;

  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await (fetchImplementation ?? globalThis.fetch)(
      `${baseUrl}/api/v1${path}`,
      init,
    );
    if (!response.ok) {
      throw new ApiError(response.status, await errorBody(response));
    }
    return (await response.json()) as T;
  }

  return {
    documents: {
      create(input: File | PasteIn): Promise<DocumentOut> {
        if (input instanceof File) {
          const form = new FormData();
          form.set("file", input);
          return request("/documents", { method: "POST", body: form });
        }
        return request("/documents", jsonRequest("POST", input));
      },
      list(): Promise<DocumentList> {
        return request("/documents");
      },
      get(documentId: string): Promise<DocumentOut> {
        return request(`/documents/${encodeURIComponent(documentId)}`);
      },
      async file(documentId: string): Promise<Blob> {
        const response = await (fetchImplementation ?? globalThis.fetch)(
          `${baseUrl}/api/v1/documents/${encodeURIComponent(documentId)}/file`,
        );
        if (!response.ok) throw new ApiError(response.status, await errorBody(response));
        return response.blob();
      },
      /** The pasted text of a text document; clause offsets index into this exact string. */
      async text(documentId: string): Promise<string> {
        const response = await (fetchImplementation ?? globalThis.fetch)(
          `${baseUrl}/api/v1/documents/${encodeURIComponent(documentId)}/file`,
        );
        if (!response.ok) throw new ApiError(response.status, await errorBody(response));
        return response.text();
      },
    },
    reviews: {
      start(input: ReviewIn): Promise<ReviewOut> {
        return request("/reviews", jsonRequest("POST", input));
      },
      get(reviewId: string): Promise<ReviewOut> {
        return request(`/reviews/${encodeURIComponent(reviewId)}`);
      },
      findings(reviewId: string): Promise<FindingsOut> {
        return request(`/reviews/${encodeURIComponent(reviewId)}/findings`);
      },
      /** Re-run only the explanation for a void verdict without one: one paid model call. */
      explainClause(reviewId: string, position: number): Promise<Finding> {
        return request(`/reviews/${encodeURIComponent(reviewId)}/clauses/${position}/explain`, {
          method: "POST",
        });
      },
      retryClause(reviewId: string, position: number): Promise<ReviewOut> {
        return request(`/reviews/${encodeURIComponent(reviewId)}/clauses/${position}/retry`, {
          method: "POST",
        });
      },
      decide(reviewId: string, position: number, input: DecisionIn): Promise<Decision> {
        return request(
          `/reviews/${encodeURIComponent(reviewId)}/clauses/${position}/decision`,
          jsonRequest("PUT", input),
        );
      },
    },
    feedback(input: FeedbackIn): Promise<FeedbackOut> {
      return request("/feedback", jsonRequest("POST", input));
    },
    statute(chunkId: string): Promise<StatuteOut> {
      return request(`/statutes/${encodeURIComponent(chunkId)}`);
    },
    health(): Promise<Health> {
      return request("/health");
    },
    ready(): Promise<Ready> {
      return request("/ready");
    },
  };
}

async function errorBody(response: Response): Promise<ErrorBody> {
  try {
    const body = (await response.json()) as Partial<ErrorBody>;
    if (typeof body.code === "string" && typeof body.message === "string") {
      return { code: body.code, message: body.message };
    }
  } catch {
    // A proxy or failed dependency can return HTML; callers still receive one stable shape.
  }
  return { code: "http_error", message: response.statusText || `HTTP ${response.status}` };
}

function jsonRequest(method: "POST" | "PUT", body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = createApiClient();
export type ApiClient = ReturnType<typeof createApiClient>;

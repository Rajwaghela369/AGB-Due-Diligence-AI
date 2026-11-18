/** Typed access to the frozen Phase 5 endpoint surface and its uniform errors. */

import type {
  AgreementOut,
  AuditOut,
  ChatIn,
  DecisionIn,
  DecisionOut,
  DeletionOut,
  DocumentList,
  DocumentOut,
  ErrorBody,
  FeedbackIn,
  FeedbackOut,
  Finding,
  FindingsOut,
  Health,
  PasteIn,
  PrincipalOut,
  Ready,
  ReviewIn,
  ReviewOut,
  ReviewSummary,
  StatuteOut,
} from "./types.gen";

/** `GET …/export?format=`: the memo as Word or Markdown, or the findings as CSV. */
export type ExportFormat = "docx" | "md" | "csv";

/** A downloaded export; `draft` is the server's `X-Export-Draft`. */
export interface ExportDownload {
  blob: Blob;
  filename: string;
  draft: boolean;
}

/**
 * Every error body has `code` and `message`; some carry more (409 `export_blocked` is an
 * `ExportBlockedBody` with `reason` and `blocking`), which is kept as sent.
 */
export type ApiErrorBody = ErrorBody & Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody;

  constructor(status: number, body: ApiErrorBody) {
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
      create(input: File | PasteIn, documentType: "agb" | "avv" = "agb"): Promise<DocumentOut> {
        if (input instanceof File) {
          const form = new FormData();
          form.set("file", input);
          form.set("document_type", documentType);
          return request("/documents", { method: "POST", body: form });
        }
        return request(
          "/documents",
          jsonRequest("POST", { ...input, document_type: input.document_type ?? documentType }),
        );
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
      /**
       * Delete a document and everything only it used (Phase 9). `dryRun` returns what would
       * go without deleting; 409 `deletion_blocked` while a review of it is running.
       */
      delete(documentId: string, options: { dryRun?: boolean } = {}): Promise<DeletionOut> {
        const query = options.dryRun ? "?dry_run=true" : "";
        return request(`/documents/${encodeURIComponent(documentId)}${query}`, {
          method: "DELETE",
        });
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
      /**
       * Record, change or replace the decision on one finding. `reviewer` is the name typed in
       * the UI; it travels percent-encoded because header values are Latin-1 only.
       */
      decide(
        reviewId: string,
        position: number,
        input: DecisionIn,
        reviewer?: string | null,
      ): Promise<DecisionOut> {
        const name = reviewer?.trim();
        return request(
          `/reviews/${encodeURIComponent(reviewId)}/clauses/${position}/decision`,
          jsonRequest("PUT", input, name ? { "X-Reviewer": encodeURIComponent(name) } : undefined),
        );
      },
      /** The summary block (Phase 8, 8.A); deterministic from the decisions. */
      summary(reviewId: string): Promise<ReviewSummary> {
        return request(`/reviews/${encodeURIComponent(reviewId)}/summary`);
      },
      /**
       * Download the DD export. Without `draft` it fails with 409 `export_blocked` while a
       * void or review finding is undecided (`ApiError.body.blocking` lists them).
       */
      async export(
        reviewId: string,
        format: ExportFormat,
        options: { draft?: boolean; reviewer?: string | null } = {},
      ): Promise<ExportDownload> {
        const query = new URLSearchParams({ format });
        if (options.draft) query.set("draft", "true");
        const name = options.reviewer?.trim();
        const response = await (fetchImplementation ?? globalThis.fetch)(
          `${baseUrl}/api/v1/reviews/${encodeURIComponent(reviewId)}/export?${query.toString()}`,
          name ? { headers: { "X-Reviewer": encodeURIComponent(name) } } : undefined,
        );
        if (!response.ok) throw new ApiError(response.status, await errorBody(response));
        return {
          blob: await response.blob(),
          filename:
            attachmentName(response.headers.get("Content-Disposition")) ?? `export.${format}`,
          draft: response.headers.get("X-Export-Draft") === "true",
        };
      },
      /**
       * Open the rewrite chat stream for one finding (Phase 8, 8.C). Resolves with the raw
       * `text/event-stream` response once the server accepted the request; errors before
       * streaming (404, 409 `not_chattable`, 422, 429, 501, 503) throw `ApiError`. The
       * caller reads `response.body` (docs/Events.md, "Rewrite chat").
       */
      async chat(
        reviewId: string,
        position: number,
        input: ChatIn,
        options: { reviewer?: string | null; signal?: AbortSignal } = {},
      ): Promise<Response> {
        const name = options.reviewer?.trim();
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (name) headers["X-Reviewer"] = encodeURIComponent(name);
        const response = await (fetchImplementation ?? globalThis.fetch)(
          `${baseUrl}/api/v1/reviews/${encodeURIComponent(reviewId)}/clauses/${position}/chat`,
          { ...jsonRequest("POST", input, headers), signal: options.signal ?? null },
        );
        if (!response.ok) throw new ApiError(response.status, await errorBody(response));
        return response;
      },
      /** The decision history, oldest first; `position` narrows it to one finding. */
      audit(reviewId: string, position?: number): Promise<AuditOut> {
        const query = position === undefined ? "" : `?position=${position}`;
        return request(`/reviews/${encodeURIComponent(reviewId)}/audit${query}`);
      },
    },
    metrics: {
      /** Lawyer agreement for one agent version (the service's current one by default). */
      agreement(agentVersion?: string): Promise<AgreementOut> {
        const query = agentVersion ? `?agent_version=${encodeURIComponent(agentVersion)}` : "";
        return request(`/metrics/agreement${query}`);
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
    /** Who the server takes the caller to be; the default owner while login is off. */
    me(): Promise<PrincipalOut> {
      return request("/me");
    },
  };
}

async function errorBody(response: Response): Promise<ApiErrorBody> {
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (typeof body.code === "string" && typeof body.message === "string") {
      return { ...body, code: body.code, message: body.message };
    }
  } catch {
    // A proxy or failed dependency can return HTML; callers still receive one stable shape.
  }
  return { code: "http_error", message: response.statusText || `HTTP ${response.status}` };
}

/** The filename of `Content-Disposition: attachment; filename*=UTF-8''…`. */
export function attachmentName(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      return null;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1] ?? null;
}

function jsonRequest(
  method: "POST" | "PUT",
  body: unknown,
  headers: Record<string, string> = {},
): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
  };
}

export const api = createApiClient();
export type ApiClient = ReturnType<typeof createApiClient>;

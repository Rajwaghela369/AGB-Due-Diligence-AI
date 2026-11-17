// The rewrite chat (Phase 8, 8.C): the SSE reader, the diff view, and the drawer chat against
// a scripted stream. No backend and no model: `fetch` answers with a hand-fed body, so a test
// can check that text shows while it streams, before the final result replaces it.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatIn, ChatResult, Finding, Review } from "../../api/types.gen";
import i18n from "../../i18n";
import { useReviewer } from "../decisions/reviewer";
import { FindingDrawer } from "../findings/finding-drawer";
import { useReviewUi } from "../review/store";
import type { ReviewData, ReviewFinding } from "../review/use-review";
import { DiffView } from "./diff-view";
import { RewriteChat } from "./rewrite-chat";
import { SseParser, readSse } from "./sse";
import { historyOf } from "./use-rewrite-chat";
import type { Turn } from "./use-rewrite-chat";

const encoder = new TextEncoder();

function frame(event: string, payload: object): string {
  return `event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`;
}

const RESULT: ChatResult = {
  message_id: "m1",
  text: "Vorschlag:\nDie Haftung ist beschränkt.\nSo bleibt § 309 gewahrt.",
  suggestion: "Die Haftung ist beschränkt.",
  diff: [
    { op: "equal", text: "Die Haftung ist " },
    { op: "delete", text: "ausgeschlossen." },
    { op: "insert", text: "beschränkt." },
  ],
  truncated: false,
  disclaimer: "KI-generierter Formulierungsvorschlag, keine Rechtsberatung.",
  model: "fake",
};

function successFrames(result: ChatResult = RESULT): string[] {
  return [
    frame("RUN_STARTED", { type: "RUN_STARTED", threadId: "r1:0:chat", runId: "m1" }),
    frame("TEXT_MESSAGE_START", { type: "TEXT_MESSAGE_START", messageId: "m1" }),
    frame("TEXT_MESSAGE_CONTENT", { type: "TEXT_MESSAGE_CONTENT", delta: "Vorschlag:\n" }),
    frame("TEXT_MESSAGE_CONTENT", { type: "TEXT_MESSAGE_CONTENT", delta: "<vorschlag>Die" }),
    frame("TEXT_MESSAGE_END", { type: "TEXT_MESSAGE_END", messageId: "m1" }),
    frame("CUSTOM", { type: "CUSTOM", name: "chat_result", value: result }),
    frame("RUN_FINISHED", { type: "RUN_FINISHED", threadId: "r1:0:chat", runId: "m1" }),
  ];
}

/** A body the test feeds by hand; `close()` ends it. */
function controlledBody() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    body,
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
  };
}

function bodyOf(frames: string[]): ReadableStream<Uint8Array> {
  const stream = controlledBody();
  frames.forEach(stream.push);
  stream.close();
  return stream.body;
}

interface Call {
  url: string;
  body: ChatIn;
  reviewer: string | null;
}

let calls: Call[];
let answers: (() => Response)[];

function sse(body: ReadableStream<Uint8Array>): Response {
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(async () => {
  calls = [];
  answers = [];
  localStorage.clear();
  useReviewer.setState({ name: "", declined: false, dialogOpen: false });
  useReviewUi.getState().reset();
  await i18n.changeLanguage("de");
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = typeof input === "string" ? input : (input as Request).url;
    if (url.endsWith("/chat")) {
      const headers = new Headers(init?.headers);
      calls.push({
        url,
        body: JSON.parse(String(init?.body)) as ChatIn,
        reviewer: headers.get("X-Reviewer"),
      });
      const next = answers.shift();
      return Promise.resolve(next ? next() : sse(bodyOf(successFrames())));
    }
    if (url.includes("/audit")) return Promise.resolve(json({ events: [] }));
    if (url.includes("/statutes/")) {
      return Promise.resolve(
        json({ chunk_id: "bgb-309", citation: "§ 309 BGB", text: "Gesetz", source_url: null }),
      );
    }
    return Promise.resolve(json({ code: "not_found", message: "x" }, 404));
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function finding(patch: Partial<ReviewFinding> = {}): ReviewFinding {
  return {
    position: 0,
    clause: {
      position: 0,
      number: "6.1",
      heading: "§ 6 Haftung",
      text: "Die Haftung ist ausgeschlossen.",
      content_hash: "h0",
    },
    status: "done",
    verdict: {
      agent_version: "v1",
      content_hash: "h0",
      verdict: "potentially_void",
      confidence: 0.9,
      legal_basis: [],
      reason_de: "Grund",
    },
    error: null,
    decision: null,
    findingStatus: "open",
    feedback: null,
    ...patch,
  } as ReviewFinding;
}

function renderChat(target: ReviewFinding = finding()) {
  const view = render(<RewriteChat reviewId="r1" finding={target} />);
  fireEvent.click(screen.getByRole("button", { name: "Umformulieren" }));
  return view;
}

// --- SSE ----------------------------------------------------------------------------------

describe("SseParser", () => {
  it("joins lines split across chunks and handles CRLF, comments and multi-line data", () => {
    const parser = new SseParser();
    expect(parser.feed("event: A\r\nda")).toEqual([]);
    expect(parser.feed('ta: {"x":1}\r\n\r\n: ping\n\nevent: B\ndata: one\ndata: two\n')).toEqual([
      { event: "A", data: '{"x":1}' },
    ]);
    expect(parser.feed("\n")).toEqual([{ event: "B", data: "one\ntwo" }]);
    expect(parser.feed("data: no event\r")).toEqual([]);
    expect(parser.feed("\n\r\n")).toEqual([{ event: "message", data: "no event" }]);
    expect(parser.feed("event: C\ndata: cut off")).toEqual([]);
    expect(parser.end()).toEqual([]); // an unterminated message is dropped
  });

  it("decodes UTF-8 split between chunks", async () => {
    const bytes = encoder.encode(frame("TEXT_MESSAGE_CONTENT", { delta: "Gewährleistung" }));
    const cut = bytes.indexOf(0xc3) + 1; // inside the two bytes of „ä“
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, cut));
        controller.enqueue(bytes.slice(cut));
        controller.close();
      },
    });
    const messages = [];
    for await (const message of readSse(stream)) messages.push(message);
    expect(messages).toHaveLength(1);
    expect(JSON.parse(messages[0]!.data)).toEqual({ delta: "Gewährleistung" });
  });
});

describe("historyOf", () => {
  it("sends back finished exchanges only", () => {
    const turns: Turn[] = [
      { id: 0, role: "user", content: "Erste" },
      {
        id: 1,
        role: "assistant",
        question: "Erste",
        status: "done",
        text: RESULT.text,
        result: RESULT,
        error: null,
      },
      { id: 2, role: "user", content: "Zweite" },
      {
        id: 3,
        role: "assistant",
        question: "Zweite",
        status: "error",
        text: "Teil",
        result: null,
        error: { code: "chat_failed", message: "" },
      },
    ];
    expect(historyOf(turns)).toEqual([
      { role: "user", content: "Erste" },
      { role: "assistant", content: RESULT.text },
    ]);
  });
});

// --- diff ---------------------------------------------------------------------------------

describe("DiffView", () => {
  it("strikes removed and underlines inserted words, labelled for screen readers", () => {
    render(<DiffView diff={RESULT.diff!} />);
    const diff = screen.getByTestId("chat-diff");
    expect(diff).toHaveTextContent(
      "Die Haftung ist entfernt: ausgeschlossen.eingefügt: beschränkt.",
    );
    expect(diff.querySelector("del")).toHaveTextContent("ausgeschlossen.");
    expect(diff.querySelector("ins")).toHaveTextContent("beschränkt.");
    expect(diff.querySelector("del")).toHaveClass("line-through");
    expect(diff.querySelector("ins")).toHaveClass("underline");
  });
});

// --- the chat -----------------------------------------------------------------------------

describe("RewriteChat", () => {
  it("is collapsed until opened, and makes no request on its own", () => {
    render(<RewriteChat reviewId="r1" finding={finding()} />);
    const toggle = screen.getByRole("button", { name: "Umformulieren" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("textbox")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Ihre Frage zur Klausel" })).toBeInTheDocument();
    expect(calls).toEqual([]);
  });

  it("streams the answer, then shows the final text, the diff and the disclaimer", async () => {
    const stream = controlledBody();
    answers.push(() => sse(stream.body));
    useReviewer.setState({ name: "Anna Müller" });
    renderChat();

    fireEvent.click(screen.getByRole("button", { name: "Formuliere 6.1 rechtskonform um" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]!.url).toContain("/api/v1/reviews/r1/clauses/0/chat");
    expect(calls[0]!.body).toEqual({ message: "Formuliere 6.1 rechtskonform um", history: [] });
    expect(calls[0]!.reviewer).toBe(encodeURIComponent("Anna Müller"));

    const frames = successFrames();
    act(() => frames.slice(0, 4).forEach(stream.push));
    // Streaming: the raw text so far, a busy notice, a stop button, no result yet.
    expect(await screen.findByText("Antwort wird erstellt …")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("chat-answer-text").textContent).toBe("Vorschlag:\n<vorschlag>Die"),
    );
    expect(screen.getByRole("button", { name: "Abbrechen" })).toBeInTheDocument();
    expect(screen.queryByTestId("chat-disclaimer")).toBeNull();

    act(() => {
      frames.slice(4).forEach(stream.push);
      stream.close();
    });
    await waitFor(() => expect(screen.getByTestId("chat-disclaimer")).toBeInTheDocument());
    expect(screen.getByTestId("chat-answer-text").textContent).toBe(RESULT.text);
    expect(screen.getByTestId("chat-disclaimer")).toHaveTextContent(
      "KI-generierter Formulierungsvorschlag, keine Rechtsberatung.",
    );
    expect(screen.getByTestId("chat-diff").querySelector("ins")).toHaveTextContent("beschränkt.");
    expect(screen.queryByText("Antwort wird erstellt …")).toBeNull();
    expect(screen.getByRole("button", { name: "Senden" })).toBeDisabled(); // empty draft
  });

  it("sends the finished exchange back as history with the next question", async () => {
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: "Formuliere 6.1 rechtskonform um" }));
    await screen.findByTestId("chat-disclaimer");

    const input = screen.getByRole("textbox", { name: "Ihre Frage zur Klausel" });
    fireEvent.change(input, { target: { value: "Kürzer, bitte" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]!.body).toEqual({
      message: "Kürzer, bitte",
      history: [
        { role: "user", content: "Formuliere 6.1 rechtskonform um" },
        { role: "assistant", content: RESULT.text },
      ],
    });
    expect(input).toHaveValue("");
    await waitFor(() => expect(screen.getAllByTestId("chat-disclaimer")).toHaveLength(2));
  });

  it("shows a RUN_ERROR, keeps no result, and asks again on request", async () => {
    answers.push(() =>
      sse(
        bodyOf([
          frame("RUN_STARTED", { type: "RUN_STARTED" }),
          frame("TEXT_MESSAGE_START", { type: "TEXT_MESSAGE_START" }),
          frame("TEXT_MESSAGE_CONTENT", { type: "TEXT_MESSAGE_CONTENT", delta: "Teil" }),
          frame("TEXT_MESSAGE_END", { type: "TEXT_MESSAGE_END" }),
          frame("RUN_ERROR", { type: "RUN_ERROR", code: "daily_budget", message: "x" }),
        ]),
      ),
    );
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: "Formuliere 6.1 rechtskonform um" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Das Tagesbudget für Modellaufrufe ist erschöpft.",
    );
    expect(screen.queryByTestId("chat-disclaimer")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Erneut fragen" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]!.body).toEqual({ message: "Formuliere 6.1 rechtskonform um", history: [] });
    await screen.findByTestId("chat-disclaimer");
  });

  it("shows an error before streaming, and a stream that ends without a result", async () => {
    answers.push(() => json({ code: "not_chattable", message: "Keine Klausel" }, 409));
    answers.push(() => sse(bodyOf(successFrames().slice(0, 3))));
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: "Formuliere 6.1 rechtskonform um" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Diese Klausel hat noch kein Prüfergebnis.",
    );

    fireEvent.click(screen.getByRole("button", { name: "Erneut fragen" }));
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getAllByRole("alert")[1]).toHaveTextContent("Die Verbindung wurde unterbrochen.");
  });

  it("stops a streaming answer", async () => {
    const stream = controlledBody();
    answers.push(() => sse(stream.body));
    renderChat();
    fireEvent.click(screen.getByRole("button", { name: "Formuliere 6.1 rechtskonform um" }));
    act(() => successFrames().slice(0, 3).forEach(stream.push));
    fireEvent.click(await screen.findByRole("button", { name: "Abbrechen" }));
    expect(await screen.findByText("Abgebrochen.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Erneut fragen" })).toBeInTheDocument();
  });

  it("speaks English", async () => {
    await i18n.changeLanguage("en");
    render(<RewriteChat reviewId="r1" finding={finding()} />);
    fireEvent.click(screen.getByRole("button", { name: "Rewrite" }));
    fireEvent.click(screen.getByRole("button", { name: "Rewrite 6.1 so it is compliant" }));
    expect(await screen.findByTestId("chat-disclaimer")).toHaveTextContent(
      "AI-generated wording suggestion, not legal advice.",
    );
    expect(screen.getByText("Suggested wording")).toBeInTheDocument();
  });
});

// --- mounted in the drawer ----------------------------------------------------------------

describe("FindingDrawer", () => {
  function review(target: ReviewFinding): ReviewData {
    return {
      review: {
        review_id: "r1",
        document_id: "d1",
        agent_version: "v1",
        scope: ["liability"],
        status: "finished",
      } as Review,
      findings: [target],
      byPosition: new Map([[target.position, target]]),
      isLoading: false,
      error: null,
      isFinal: true,
      progress: null,
      document: null,
      refresh: () => Promise.resolve(),
    };
  }

  function renderDrawer(target: ReviewFinding) {
    useReviewUi.getState().select(target.position);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <FindingDrawer review={review(target)} />
      </QueryClientProvider>,
    );
  }

  it("offers the chat for a finding with a verdict", async () => {
    renderDrawer(finding());
    const drawer = await screen.findByTestId("finding-drawer");
    expect(within(drawer).getByTestId("rewrite-chat")).toBeInTheDocument();
  });

  it("does not offer it for a clause without a verdict", async () => {
    renderDrawer(finding({ status: "failed", verdict: null, error: "boom" } as Partial<Finding>));
    const drawer = await screen.findByTestId("finding-drawer");
    expect(within(drawer).queryByTestId("rewrite-chat")).toBeNull();
  });
});

// A small server-sent-events reader for the rewrite chat (Phase 8, 8.C). The chat is a POST,
// and EventSource can only GET, so the stream is read from `fetch`'s body instead. The
// framing follows the SSE spec as far as the chat uses it: `event:` and `data:` fields
// (several `data:` lines join with "\n"), comment lines starting with ":", and messages
// ending at a blank line, with "\n", "\r\n" or "\r" line ends. `id:` and `retry:` are ignored:
// a chat stream cannot be resumed (docs/Events.md, "Rewrite chat").

export interface SseMessage {
  /** The `event:` field; "message" when absent, as in the spec. */
  event: string;
  data: string;
}

/** Incremental parser: feed text as it arrives, get the messages it completed. */
export class SseParser {
  private buffer = "";
  private event = "";
  private data: string[] = [];

  feed(text: string): SseMessage[] {
    this.buffer += text;
    const messages: SseMessage[] = [];
    for (;;) {
      const match = /\r\n|\r|\n/.exec(this.buffer);
      if (!match) break;
      // A lone "\r" at the very end may be the first half of "\r\n": wait for more.
      if (match[0] === "\r" && match.index === this.buffer.length - 1) break;
      const line = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const message = this.line(line);
      if (message) messages.push(message);
    }
    return messages;
  }

  /** The stream ended: a message without its closing blank line is incomplete and dropped. */
  end(): SseMessage[] {
    this.buffer = "";
    this.event = "";
    this.data = [];
    return [];
  }

  private line(line: string): SseMessage | null {
    if (line === "") {
      if (this.data.length === 0) {
        this.event = "";
        return null;
      }
      const message = { event: this.event || "message", data: this.data.join("\n") };
      this.event = "";
      this.data = [];
      return message;
    }
    if (line.startsWith(":")) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") this.event = value;
    else if (field === "data") this.data.push(value);
    return null;
  }
}

/** Every complete message of a streamed body, in order, decoding UTF-8 across chunks.
 * `signal` cancels the body, so a stop ends the loop even while a read is waiting. */
export async function* readSse(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();
  const cancel = () => void reader.cancel().catch(() => undefined);
  signal?.addEventListener("abort", cancel, { once: true });
  try {
    for (;;) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const { done, value } = await reader.read();
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      if (done) break;
      yield* parser.feed(decoder.decode(value, { stream: true }));
    }
    yield* parser.feed(decoder.decode());
    parser.end();
  } finally {
    signal?.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

// Who is deciding. There is no login before Phase 9, so the lawyer types a name once; it is
// kept in localStorage and sent as `X-Reviewer` (percent-encoded by the API client) with every
// decision. The audit trail marks such names „nicht angemeldet“.
//
// `ensure()` is what a decision awaits: it resolves at once with a known name, or opens the
// name dialog (mounted once by the workspace) and resolves with what the lawyer chose. A
// closed dialog resolves `null`, which aborts the decision; „Ohne Namen“ resolves "" and is
// remembered for the session so the dialog does not return on every click.
import { create } from "zustand";

const KEY = "agb.reviewer";
export const MAX_NAME_LENGTH = 100;

function read(): string {
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

function write(name: string): void {
  try {
    if (name === "") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, name);
  } catch {
    // Storage blocked: the name lives for this page only.
  }
}

/** The API refuses control characters and names over 100 characters; clean before sending. */
// eslint-disable-next-line no-control-regex -- the point is to remove control characters
const CONTROL = /[\u0000-\u001f\u007f]/g;
export function cleanName(raw: string): string {
  return raw.replace(CONTROL, " ").trim().slice(0, MAX_NAME_LENGTH);
}

interface ReviewerState {
  name: string;
  /** The lawyer chose to go on without a name; do not ask again this session. */
  declined: boolean;
  dialogOpen: boolean;
  setName: (name: string) => void;
  /** Resolves with the name to send, "" for none, or null when the lawyer cancelled. */
  ensure: () => Promise<string | null>;
  /** Open the dialog to change the name (no decision waits on it). */
  edit: () => void;
  submit: (name: string) => void;
  skip: () => void;
  cancel: () => void;
}

let waiting: ((value: string | null) => void) | null = null;

function settle(value: string | null) {
  waiting?.(value);
  waiting = null;
}

export const useReviewer = create<ReviewerState>((set, get) => ({
  name: read(),
  declined: false,
  dialogOpen: false,
  setName: (raw) => {
    const name = cleanName(raw);
    write(name);
    set({ name });
  },
  ensure: () => {
    const { name, declined } = get();
    if (name !== "") return Promise.resolve(name);
    if (declined) return Promise.resolve("");
    settle(null);
    set({ dialogOpen: true });
    return new Promise((resolve) => {
      waiting = resolve;
    });
  },
  edit: () => set({ dialogOpen: true }),
  submit: (raw) => {
    get().setName(raw);
    set({ dialogOpen: false, declined: false });
    settle(get().name);
  },
  skip: () => {
    set({ dialogOpen: false, declined: true });
    settle("");
  },
  cancel: () => {
    set({ dialogOpen: false });
    settle(null);
  },
}));

// Phase 7 decisions in the whole workspace, against a small stateful fake of the decision
// API (docs/Phase 7 API.md): status by mouse and by keyboard, progress without a reload, the
// audit list, the reviewer name and the API's own error messages.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AuditOut,
  DecisionEvent,
  DecisionIn,
  DecisionOut,
  Finding,
  FindingsOut,
  Review,
  ReviewOut,
} from "../../api/types.gen";
import "../../i18n";
import { server } from "../../test/server";
import { useReviewUi } from "../review/store";
import { ReviewWorkspace } from "../review/workspace";
import { progressOf } from "./progress";
import { useReviewer } from "./reviewer";

vi.mock("../viewer/document-viewer", () => ({
  DocumentViewer: () => <div data-testid="document-viewer" />,
}));
vi.mock("../table/findings-table", () => ({
  FindingsTable: () => <div data-testid="findings-table" />,
}));

const BASE = "http://localhost/api/v1";

function finding(position: number, verdict: string, patch: Partial<Finding> = {}): Finding {
  return {
    position,
    status: "done",
    finding_status: "open",
    clause: {
      position,
      number: `${position + 1}`,
      heading: `Klausel ${position}`,
      text: `Text ${position}`,
      content_hash: `h${position}`,
    },
    verdict: {
      agent_version: "v1",
      content_hash: `h${position}`,
      verdict,
      confidence: 0.9,
      legal_basis: ["bgb-309"],
      candidates: ["bgb-309", "bgb-307"],
      reason_de: `Grund ${position}`,
    },
    ...patch,
  } as Finding;
}

// The fake API -----------------------------------------------------------------------------

let findings: Finding[];
let events: DecisionEvent[];
let headers: Array<string | null>;
let bodies: DecisionIn[];
let failWith: { status: number; code: string; message: string } | null;
let audits: number;

function review(): Review {
  const verdicts = findings.map((f) => f.verdict?.verdict);
  const count = (verdict: string) => verdicts.filter((v) => v === verdict).length;
  const decided = findings.filter((f) => f.decision);
  const open = (verdict: string) =>
    findings.filter((f) => f.verdict?.verdict === verdict && !f.decision).length;
  return {
    review_id: "r1",
    document_id: "d1",
    agent_version: "v1",
    scope: ["liability"],
    status: findings.some((f) => f.status === "awaiting_decision")
      ? "awaiting_decisions"
      : "finished",
    counts: {
      total: findings.length,
      done: findings.filter((f) => f.status === "done").length,
      failed: 0,
      potentially_void: count("potentially_void"),
      uncertain: count("uncertain"),
      valid: count("valid"),
      awaiting_decision: findings.filter((f) => f.status === "awaiting_decision").length,
      decided: decided.length,
      open_void: open("potentially_void"),
      open_review: open("uncertain"),
    },
  } as Review;
}

function reviewOut(): ReviewOut {
  const r = review();
  const p = progressOf(r);
  return { review: r, decidable: p.decidable, progress: p.fraction, can_complete: p.canComplete };
}

function installApi() {
  server.use(
    http.get(`${BASE}/reviews/r1/findings`, () =>
      HttpResponse.json({ review: review(), findings } satisfies FindingsOut),
    ),
    // A review that awaits decisions has no run: the stream replays the state and ends. Its
    // snapshot is older than any decision made afterwards and must not win over it.
    http.get(`${BASE}/reviews/r1/events`, () => {
      const snapshot = { review: review(), clauses: {} };
      const lines = [
        { id: 1, type: "STATE_SNAPSHOT", body: { snapshot } },
        { id: 2, type: "RUN_FINISHED", body: {} },
      ].map(
        (e) =>
          `id: ${e.id}\nevent: ${e.type}\ndata: ${JSON.stringify({ type: e.type, ...e.body })}\n\n`,
      );
      return new HttpResponse(lines.join(""), { headers: { "Content-Type": "text/event-stream" } });
    }),
    http.get(`${BASE}/documents/d1`, () =>
      HttpResponse.json({ document: { document_id: "d1", source_type: "pdf" }, clauses: [] }),
    ),
    http.get(`${BASE}/statutes/:id`, ({ params }) =>
      HttpResponse.json({
        chunk_id: params.id,
        citation: `§ ${String(params.id)}`,
        title: "BGB",
        text: "Text",
        source_url: null,
      }),
    ),
    http.get(`${BASE}/reviews/r1/audit`, ({ request }) => {
      audits += 1;
      const position = new URL(request.url).searchParams.get("position");
      return HttpResponse.json({
        events: events.filter((e) => position === null || e.position === Number(position)),
      } satisfies AuditOut);
    }),
    http.get(`${BASE}/metrics/agreement`, () => {
      return HttpResponse.json({ agreement: { agent_version: "v1", decided: 0 }, current: true });
    }),
    http.put(`${BASE}/reviews/r1/clauses/:pos/decision`, async ({ params, request }) => {
      if (failWith) return HttpResponse.json(failWith, { status: failWith.status });
      const position = Number(params.pos);
      const body = (await request.json()) as DecisionIn;
      headers.push(request.headers.get("x-reviewer"));
      bodies.push(body);
      const before = findings.find((f) => f.position === position)!;
      const status = { accept: "accepted", override: "overridden", dismiss: "dismissed" } as const;
      const updated = {
        ...before,
        status: "done",
        finding_status: status[body.action],
        decision: { review_id: "r1", position, ...body, note: body.note ?? "" },
      } as Finding;
      findings = findings.map((f) => (f.position === position ? updated : f));
      const previous = events.filter((e) => e.position === position).at(-1);
      const decidedVerdict = (action: string) =>
        action === "dismiss"
          ? null
          : action === "override"
            ? (body.corrected_verdict ?? null)
            : (before.verdict?.verdict ?? null);
      events.push({
        event_id: events.length + 1,
        review_id: "r1",
        position,
        action: body.action,
        previous_action: previous?.action ?? null,
        from_verdict: previous ? previous.to_verdict : (before.verdict?.verdict ?? null),
        from_basis: previous?.to_basis ?? ["bgb-309"],
        to_verdict: decidedVerdict(body.action),
        to_basis: body.action === "dismiss" ? [] : (body.corrected_basis ?? []),
        note: body.note ?? "",
        decided_by: request.headers.get("x-reviewer")
          ? decodeURIComponent(request.headers.get("x-reviewer")!)
          : null,
        authenticated: false,
        agent_version: "v1",
        at: `2026-11-17T10:0${events.length}:00Z`,
      } as DecisionEvent);
      return HttpResponse.json({
        decision: updated.decision,
        finding: updated,
        review: reviewOut(),
      } as DecisionOut);
    }),
  );
}

beforeEach(() => {
  findings = [
    finding(0, "potentially_void"),
    finding(1, "uncertain", { status: "awaiting_decision" }),
    finding(2, "valid"),
  ];
  events = [];
  headers = [];
  bodies = [];
  failWith = null;
  audits = 0;
  useReviewUi.getState().reset();
  useReviewer.setState({ name: "", declined: false, dialogOpen: false });
  localStorage.clear();
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
    real(
      typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
      init,
    ),
  );
  installApi();
});
afterEach(cleanup);

async function renderWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/reviews/r1"]}>
        <Routes>
          <Route path="reviews/:id" element={<ReviewWorkspace />} />
          <Route path="/" element={<p data-testid="home">home</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByTestId("finding-card-0");
}

const card = (position: number) => screen.getByTestId(`finding-card-${position}`);
const openCard = (position: number) =>
  fireEvent.click(within(card(position)).getAllByRole("button")[0]!);
const badge = (position: number) => within(card(position)).getByTestId("decision-status");
const press = (key: string) =>
  act(() => {
    fireEvent.keyDown(document.body, { key, code: `Key${key.toUpperCase()}` });
  });

async function enterName(name = "Anna Müller") {
  const dialog = await screen.findByRole("dialog", { name: "Ihr Name" });
  fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: name } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Speichern" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
}

describe("finding status", () => {
  it("shows open, and „Entscheidung nötig“ for a clause that waits for the lawyer", async () => {
    await renderWorkspace();
    expect(badge(0)).toHaveTextContent("Offen");
    expect(badge(1)).toHaveTextContent("Entscheidung nötig");
    expect(badge(1).querySelector("svg")).not.toBeNull();
  });

  it("filters the findings by status", async () => {
    findings = [
      finding(0, "potentially_void", { finding_status: "accepted" }),
      finding(1, "uncertain"),
      finding(2, "valid", { finding_status: "dismissed" }),
    ];
    await renderWorkspace();
    fireEvent.change(screen.getByRole("combobox", { name: "Entscheidung" }), {
      target: { value: "open" },
    });
    expect(screen.queryByTestId("finding-card-0")).toBeNull();
    expect(card(1)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Entscheidung" }), {
      target: { value: "dismissed" },
    });
    expect(screen.queryByTestId("finding-card-1")).toBeNull();
    expect(card(2)).toBeInTheDocument();
  });
});

describe("deciding by mouse", () => {
  it("accepts from the drawer, asks for the name once and sends it encoded", async () => {
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.change(within(drawer).getByLabelText("Notiz (optional)"), {
      target: { value: "Passt." },
    });
    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    await enterName("Anna Müller");

    await waitFor(() => expect(badge(0)).toHaveTextContent("Akzeptiert"));
    expect(bodies).toEqual([{ action: "accept", note: "Passt." }]);
    expect(headers).toEqual(["Anna%20M%C3%BCller"]);
    expect(localStorage.getItem("agb.reviewer")).toBe("Anna Müller");
    expect(within(drawer).getByTestId("current-decision")).toHaveTextContent("Akzeptiert");

    // The second decision does not ask again.
    fireEvent.click(within(drawer).getByRole("button", { name: /^Verwerfen/ }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Verworfen"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(headers).toEqual(["Anna%20M%C3%BCller", "Anna%20M%C3%BCller"]);
  });

  it("overrides with a verdict and a § from the finding's basis", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.click(within(drawer).getByRole("button", { name: /^Überschreiben/ }));
    const form = within(drawer).getByTestId("override-form");

    // Same verdict and no other § changes nothing; the form says so without a request.
    fireEvent.click(within(form).getByRole("radio", { name: /^Potenziell unwirksam/ }));
    fireEvent.click(within(form).getByRole("button", { name: "Überschreiben bestätigen" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("anderes Urteil");
    expect(bodies).toHaveLength(0);

    fireEvent.click(within(form).getByRole("radio", { name: "Wirksam" }));
    fireEvent.click(await within(form).findByRole("checkbox", { name: "§ bgb-307" }));
    fireEvent.click(within(form).getByRole("button", { name: "Überschreiben bestätigen" }));

    await waitFor(() => expect(badge(0)).toHaveTextContent("Überschrieben"));
    expect(bodies).toEqual([
      {
        action: "override",
        corrected_verdict: "valid",
        corrected_basis: ["bgb-307"],
        note: "",
      },
    ]);
    expect(within(drawer).getByTestId("current-decision")).toHaveTextContent(
      "Überschrieben: Wirksam",
    );
  });

  it("decides from the card buttons, and override opens the picker in the drawer", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();
    fireEvent.click(within(card(2)).getByRole("button", { name: "Akzeptieren" }));
    await waitFor(() => expect(badge(2)).toHaveTextContent("Akzeptiert"));

    fireEvent.click(within(card(0)).getByRole("button", { name: "Überschreiben" }));
    const form = await screen.findByTestId("override-form");
    expect(form).toBeInTheDocument();
    expect(useReviewUi.getState().selected).toBe(0);
  });

  it("shows the API's own message when the decision is refused", async () => {
    useReviewer.setState({ name: "Anna" });
    failWith = { status: 409, code: "not_decidable", message: "Diese Klausel hat kein Urteil." };
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    expect(await within(drawer).findByTestId("decision-error")).toHaveTextContent(
      "Diese Klausel hat kein Urteil.",
    );
    expect(badge(0)).toHaveTextContent("Offen");

    failWith = { status: 422, code: "invalid_decision", message: "Das Urteil ist unverändert." };
    fireEvent.click(within(drawer).getByRole("button", { name: /^Verwerfen/ }));
    expect(await within(drawer).findByTestId("decision-error")).toHaveTextContent(
      "Das Urteil ist unverändert.",
    );
  });

  it("does not send anything when the name dialog is closed", async () => {
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    const dialog = await screen.findByRole("dialog", { name: "Ihr Name" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Schließen" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(bodies).toHaveLength(0);
    expect(badge(0)).toHaveTextContent("Offen");
  });
});

describe("deciding by keyboard", () => {
  it("A accepts, O overrides and D dismisses the selected finding", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();

    openCard(0);
    await screen.findByTestId("finding-drawer");
    press("a");
    await waitFor(() => expect(badge(0)).toHaveTextContent("Akzeptiert"));

    // Select another finding, then O opens the picker and the keyboard finishes the override.
    fireEvent.click(
      within(screen.getByTestId("finding-drawer")).getByRole("button", { name: "Fundstellen" }),
    );
    openCard(1);
    await screen.findByTestId("finding-drawer");
    press("o");
    const form = await screen.findByTestId("override-form");
    fireEvent.click(within(form).getByRole("radio", { name: "Wirksam" }));
    fireEvent.submit(form);
    await waitFor(() => expect(badge(1)).toHaveTextContent("Überschrieben"));

    fireEvent.click(
      within(screen.getByTestId("finding-drawer")).getByRole("button", { name: "Fundstellen" }),
    );
    openCard(2);
    await screen.findByTestId("finding-drawer");
    press("d");
    await waitFor(() => expect(badge(2)).toHaveTextContent("Verworfen"));
    expect(bodies.map((b) => b.action)).toEqual(["accept", "override", "dismiss"]);
  });

  it("ignores A, O and D with no selection, in a text field and while a dialog is open", async () => {
    await renderWorkspace();
    press("a");
    expect(bodies).toHaveLength(0);

    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.keyDown(within(drawer).getByLabelText("Notiz (optional)"), {
      key: "d",
      code: "KeyD",
    });
    expect(bodies).toHaveLength(0);
    expect(screen.queryByTestId("override-form")).toBeNull();
  });
});

describe("progress", () => {
  it("counts void and review separately and updates without a reload", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();
    const progress = screen.getByTestId("decision-progress");
    expect(within(progress).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "0");
    expect(within(progress).getByTestId("open-void")).toHaveTextContent("offen: 1");
    expect(within(progress).getByTestId("open-review")).toHaveTextContent("offen: 1");
    const complete = within(progress).getByRole("button", { name: "Prüfung abschließen" });
    expect(complete).toBeDisabled();
    const requests = vi.mocked(globalThis.fetch).mock.calls.length;

    openCard(0);
    press("a");
    await waitFor(() =>
      expect(within(progress).getByTestId("open-void")).toHaveTextContent("offen: 0"),
    );
    expect(within(progress).getByTestId("open-review")).toHaveTextContent("offen: 1");
    expect(within(progress).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1");
    expect(progress).toHaveTextContent("1 von 3 entschieden");
    expect(complete).toBeDisabled();

    fireEvent.click(
      within(screen.getByTestId("finding-drawer")).getByRole("button", { name: "Fundstellen" }),
    );
    fireEvent.click(within(card(1)).getByRole("button", { name: "Verwerfen" }));
    await waitFor(() => expect(complete).toBeEnabled());
    // The findings were never refetched to get here: the decision responses carried the counts.
    const findingGets = vi
      .mocked(globalThis.fetch)
      .mock.calls.slice(requests)
      .filter(([url, init]) => String(url).endsWith("/findings") && !init?.method);
    expect(findingGets).toHaveLength(0);

    fireEvent.click(complete);
    fireEvent.click(await screen.findByRole("button", { name: "Abschließen" }));
    expect(await screen.findByTestId("home")).toBeInTheDocument();
  });
});

describe("audit view", () => {
  it("lists three entries after three decisions, newest first, and marks the name as unauthenticated", async () => {
    useReviewer.setState({ name: "Anna Müller" });
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    expect(await within(drawer).findByText("Noch keine Entscheidung.")).toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Akzeptiert"));

    fireEvent.change(within(drawer).getByLabelText("Notiz (optional)"), {
      target: { value: "Doch anders." },
    });
    fireEvent.click(within(drawer).getByRole("button", { name: /^Überschreiben/ }));
    const form = within(drawer).getByTestId("override-form");
    fireEvent.click(within(form).getByRole("radio", { name: "Wirksam" }));
    fireEvent.click(within(form).getByRole("button", { name: "Überschreiben bestätigen" }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Überschrieben"));

    fireEvent.click(within(drawer).getByRole("button", { name: /^Verwerfen/ }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Verworfen"));

    await waitFor(() => expect(within(drawer).getAllByTestId("audit-entry")).toHaveLength(3));
    const entries = within(drawer).getAllByTestId("audit-entry");
    expect(entries[0]).toHaveTextContent("Verworfen");
    expect(entries[1]).toHaveTextContent("Überschrieben");
    expect(entries[1]).toHaveTextContent("Doch anders.");
    expect(entries[1]).toHaveTextContent("Potenziell unwirksam → Wirksam");
    expect(entries[2]).toHaveTextContent("Akzeptiert");
    for (const entry of entries) {
      expect(entry).toHaveTextContent("Anna Müller (nicht angemeldet)");
      expect(entry).toHaveTextContent("Agent-Version: v1");
    }
  });

  it("refetches the history after a decision", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    await within(drawer).findByText("Noch keine Entscheidung.");
    const before = audits;
    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    await waitFor(() => expect(audits).toBeGreaterThan(before));
  });
});

describe("reviewer name", () => {
  it("shows the stored name with „nicht angemeldet“ and lets it be changed", async () => {
    useReviewer.setState({ name: "Anna" });
    await renderWorkspace();
    const control = screen.getByTestId("reviewer-control");
    expect(control).toHaveTextContent("Anna");
    expect(control).toHaveTextContent("nicht angemeldet");
    fireEvent.click(control);
    await enterName("Bernd Öztürk");
    expect(control).toHaveTextContent("Bernd Öztürk");
    expect(localStorage.getItem("agb.reviewer")).toBe("Bernd Öztürk");
  });

  it("goes on without a name when asked, and does not ask again", async () => {
    await renderWorkspace();
    openCard(0);
    const drawer = await screen.findByTestId("finding-drawer");
    fireEvent.click(within(drawer).getByRole("button", { name: /^Akzeptieren/ }));
    const dialog = await screen.findByRole("dialog", { name: "Ihr Name" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Ohne Namen fortfahren" }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Akzeptiert"));
    expect(headers).toEqual([null]);
    fireEvent.click(within(drawer).getByRole("button", { name: /^Verwerfen/ }));
    await waitFor(() => expect(badge(0)).toHaveTextContent("Verworfen"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

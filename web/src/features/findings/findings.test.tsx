import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Feedback, FeedbackIn, Finding, FindingsOut, Review } from "../../api/types.gen";
import "../../i18n";
import { server } from "../../test/server";
import { useReviewUi } from "../review/store";
import { useReview } from "../review/use-review";
import { FindingDrawer } from "./finding-drawer";
import { FindingsPanel } from "./findings-panel";

const REVIEW = {
  review_id: "r1",
  document_id: "d1",
  agent_version: "v1",
  scope: ["liability"],
  status: "finished",
} as Review;

function finding(position: number, patch: Partial<Finding> = {}): Finding {
  return {
    position,
    status: "done",
    clause: {
      position,
      number: `${position + 1}.1`,
      heading: `Klausel ${position}`,
      text: `Text der Klausel ${position}`,
      content_hash: `h${position}`,
    },
    verdict: {
      agent_version: "v1",
      content_hash: `h${position}`,
      verdict: "valid",
      confidence: 0.9,
      legal_basis: [],
      candidates: ["c1", "c2"],
      basis_scores: { c1: 0.91, c2: 0.4 },
      reason_de: `Grund ${position}`,
      trace_id: "trace-1",
      observations: { judge: "obs-judge", retrieval: "obs-ret" },
    },
    ...patch,
  } as Finding;
}

function fixture(): Finding[] {
  const void0 = finding(0);
  void0.verdict = {
    ...void0.verdict!,
    verdict: "potentially_void",
    confidence: 0.85,
    legal_basis: ["bgb-307", "bgb-309"],
    candidates: ["bgb-307", "bgb-309"],
    basis_scores: { "bgb-307": 0.5, "bgb-309": 0.9 },
  };
  const review2 = finding(2);
  review2.verdict = {
    ...review2.verdict!,
    verdict: "uncertain",
    confidence: 0.65,
    reason_unavailable: true,
    reason_de: "",
  };
  return [
    void0,
    finding(1),
    review2,
    finding(3, { status: "out_of_scope", verdict: null }),
    finding(4, { status: "failed", verdict: null, error: "boom" }),
  ];
}

let stored: Finding[];
let posts: FeedbackIn[];
let statuteRequests: string[];
let retries: number[];
let explains: number[];
let explainStatus: number;

beforeEach(() => {
  stored = fixture();
  posts = [];
  statuteRequests = [];
  retries = [];
  explains = [];
  explainStatus = 200;
  useReviewUi.getState().reset();
  localStorage.clear();
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) =>
    real(
      typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
      init,
    ),
  );
  server.use(
    http.get("http://localhost/api/v1/reviews/r1/findings", () =>
      HttpResponse.json({ review: REVIEW, findings: stored } satisfies FindingsOut),
    ),
    http.get("http://localhost/api/v1/reviews/r1/audit", () => HttpResponse.json({ events: [] })),
    http.get("http://localhost/api/v1/statutes/:id", ({ params }) => {
      statuteRequests.push(String(params.id));
      return HttpResponse.json({
        chunk_id: params.id,
        citation: `§ ${String(params.id).toUpperCase()}`,
        title: "BGB",
        text: `Gesetzestext ${String(params.id)}`,
        source_url: null,
      });
    }),
    http.post("http://localhost/api/v1/feedback", async ({ request }) => {
      const body = (await request.json()) as FeedbackIn;
      posts.push(body);
      stored = stored.map((f) =>
        f.position === body.position
          ? ({
              ...f,
              feedback: { ...body, feedback_id: "f1", agent_version: "v1" } as Feedback,
            } as Finding)
          : f,
      );
      return HttpResponse.json({ feedback_id: "f1", scored_steps: ["judge"] });
    }),
    http.post("http://localhost/api/v1/reviews/r1/clauses/:pos/explain", ({ params }) => {
      explains.push(Number(params.pos));
      if (explainStatus !== 200) {
        return HttpResponse.json(
          { code: "explain_failed", message: "x" },
          { status: explainStatus },
        );
      }
      stored = stored.map((f) =>
        f.position === Number(params.pos)
          ? ({
              ...f,
              verdict: { ...f.verdict!, reason_unavailable: false, reason_de: "Neue Begründung." },
            } as Finding)
          : f,
      );
      return HttpResponse.json(stored[Number(params.pos)]);
    }),
    http.post("http://localhost/api/v1/reviews/r1/clauses/:pos/retry", ({ params }) => {
      retries.push(Number(params.pos));
      return HttpResponse.json({ review: REVIEW });
    }),
  );
});

afterEach(cleanup);

function Workspace() {
  const review = useReview("r1");
  if (review.review === null) return <p>loading</p>;
  return (
    <>
      <FindingsPanel review={review} />
      <FindingDrawer review={review} />
    </>
  );
}

function renderWorkspace() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <Workspace />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

const card = (position: number) => screen.findByTestId(`finding-card-${position}`);
const drawer = () => screen.findByTestId("finding-drawer");

describe("regenerating a missing reasoning", () => {
  function voidWithoutReason() {
    stored[0] = {
      ...stored[0]!,
      verdict: { ...stored[0]!.verdict!, reason_unavailable: true, reason_de: "" },
    } as Finding;
  }

  it("offers the button for a void verdict and shows the new reasoning after one call", async () => {
    voidWithoutReason();
    renderWorkspace();
    fireEvent.click(within(await card(0)).getAllByRole("button")[0]!);
    const panel = await drawer();
    fireEvent.click(within(panel).getByRole("button", { name: "Begründung erzeugen" }));

    expect(await within(panel).findByText("Neue Begründung.")).toBeInTheDocument();
    expect(explains).toEqual([0]);
    expect(within(panel).queryByRole("button", { name: "Begründung erzeugen" })).toBeNull();
  });

  it("keeps the notice and the button when the call fails", async () => {
    voidWithoutReason();
    explainStatus = 502;
    renderWorkspace();
    fireEvent.click(within(await card(0)).getAllByRole("button")[0]!);
    const panel = await drawer();
    fireEvent.click(within(panel).getByRole("button", { name: "Begründung erzeugen" }));

    expect(await within(panel).findByRole("alert")).toHaveTextContent(/nicht erzeugt/);
    expect(within(panel).getByRole("button", { name: "Begründung erzeugen" })).toBeEnabled();
  });

  it("does not offer it when the verdict is not void", async () => {
    renderWorkspace();
    fireEvent.click(within(await card(2)).getAllByRole("button")[0]!);
    const panel = await drawer();
    expect(within(panel).getByText("Begründung nicht verfügbar")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Begründung erzeugen" })).toBeNull();
  });
});

describe("filters and navigation", () => {
  it("lists cards in document order, hides out-of-scope clauses and counts per filter", async () => {
    renderWorkspace();
    await card(0);
    const order = screen
      .getAllByTestId(/^finding-card-/)
      .map((el) => el.getAttribute("data-testid"));
    expect(order).toEqual(["0", "1", "2", "4"].map((n) => `finding-card-${n}`));
    expect(screen.getByRole("button", { name: /Unwirksam/ })).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: /Prüfen/ })).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: /Alle/ })).toHaveTextContent("4");
    expect(useReviewUi.getState().navigable).toEqual([0, 1, 2, 4]);
  });

  it("filters to void and review and publishes the visible positions as navigable", async () => {
    renderWorkspace();
    await card(0);

    fireEvent.click(screen.getByRole("button", { name: /Unwirksam/ }));
    expect(screen.getAllByTestId(/^finding-card-/)).toHaveLength(1);
    await waitFor(() => expect(useReviewUi.getState().navigable).toEqual([0]));

    fireEvent.click(screen.getByRole("button", { name: /Prüfen/ }));
    expect(screen.getByTestId("finding-card-2")).toBeInTheDocument();
    await waitFor(() => expect(useReviewUi.getState().navigable).toEqual([2]));

    fireEvent.click(screen.getByRole("button", { name: /Alle/ }));
    await waitFor(() => expect(useReviewUi.getState().navigable).toEqual([0, 1, 2, 4]));
  });

  it("walks the filtered list with next and previous", async () => {
    renderWorkspace();
    await card(0);
    fireEvent.click(screen.getByRole("button", { name: /Alle/ }));
    act(() => useReviewUi.getState().next());
    expect(useReviewUi.getState().selected).toBe(0);
    act(() => useReviewUi.getState().next());
    expect(useReviewUi.getState().selected).toBe(1);
    expect(await drawer()).toHaveTextContent("Klausel 1");
    act(() => useReviewUi.getState().previous());
    expect(useReviewUi.getState().selected).toBe(0);
  });
});

describe("card and drawer stay in sync", () => {
  it("shows tone label, clause, strongest § and confidence on a card", async () => {
    renderWorkspace();
    const first = await card(0);
    expect(within(first).getByText("Potenziell unwirksam")).toBeInTheDocument();
    expect(within(first).getByText("1.1 · Klausel 0")).toBeInTheDocument();
    expect(within(first).getByText("Hohe Sicherheit")).toBeInTheDocument();
    expect(within(first).getByText("Grund 0")).toBeInTheDocument();
    // bgb-309 outscores bgb-307, so it is the strongest.
    expect(await within(first).findByText("§ BGB-309")).toBeInTheDocument();
    expect(within(await card(4)).getByText("Konnte nicht geprüft werden")).toBeInTheDocument();
  });

  it("hover marks the card and click opens the drawer; Esc closes it", async () => {
    renderWorkspace();
    const first = await card(0);

    fireEvent.mouseEnter(first);
    expect(useReviewUi.getState().hovered).toBe(0);
    fireEvent.mouseLeave(first);
    expect(useReviewUi.getState().hovered).toBeNull();

    fireEvent.click(within(first).getAllByRole("button")[0]!);
    expect(useReviewUi.getState().selected).toBe(0);
    expect(first).toHaveAttribute("data-selected", "true");
    const panel = await drawer();
    expect(within(panel).getByTestId("drawer-clause")).toHaveTextContent("Text der Klausel 0");

    fireEvent.keyDown(panel, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("finding-drawer")).toBeNull());
    expect(useReviewUi.getState().selected).toBeNull();
  });

  it("keyboard focus hovers a card and a selection from elsewhere opens the same drawer", async () => {
    renderWorkspace();
    const second = await card(1);
    const button = within(second).getAllByRole("button")[0]!;
    act(() => button.focus());
    expect(useReviewUi.getState().hovered).toBe(1);

    // The document or table selecting a clause reaches the panel and the drawer alike.
    act(() => useReviewUi.getState().select(2));
    expect(await drawer()).toHaveTextContent("Klausel 2");
    expect(await card(2)).toHaveAttribute("data-selected", "true");
    expect(second).toHaveAttribute("data-selected", "false");

    // Clicking another card swaps the drawer instead of dismissing it.
    fireEvent.click(within(await card(0)).getAllByRole("button")[0]!);
    await waitFor(() =>
      expect(screen.getByTestId("finding-drawer")).toHaveTextContent("Klausel 0"),
    );
  });

  it("shows the reasoning, or the unavailable notice", async () => {
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    expect(await screen.findByText("Grund 0", { selector: "p" })).toBeInTheDocument();
    act(() => useReviewUi.getState().select(2));
    await waitFor(() =>
      expect(
        within(screen.getByTestId("finding-drawer")).getByText("Begründung nicht verfügbar"),
      ).toBeInTheDocument(),
    );
  });
});

describe("drawer confidence", () => {
  it("shows the band label once, not 'Sicherheit: Hohe Sicherheit'", async () => {
    renderWorkspace();
    fireEvent.click(within(await card(0)).getAllByRole("button")[0]!);
    const panel = await drawer();
    expect(panel).toHaveTextContent("Sicherheit: Hoch");
    expect(panel).not.toHaveTextContent("Hohe Sicherheit");
  });
});

describe("statute text", () => {
  it("expands the first cited § and fetches the others on demand", async () => {
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    const panel = await drawer();
    expect(await within(panel).findByText("Gesetzestext bgb-307")).toBeInTheDocument();
    expect(within(panel).queryByText("Gesetzestext bgb-309")).toBeNull();

    fireEvent.click(within(panel).getByRole("button", { name: /BGB-309/ }));
    expect(await within(panel).findByText("Gesetzestext bgb-309")).toBeInTheDocument();
    // Cached: the card's request for bgb-309 and the drawer's share one fetch.
    expect(statuteRequests.filter((id) => id === "bgb-309")).toHaveLength(1);
    expect(statuteRequests.filter((id) => id === "bgb-307")).toHaveLength(1);
  });
});

describe("technical details", () => {
  it("is collapsed, then shows candidates, the number and the Langfuse link", async () => {
    vi.stubEnv("VITE_LANGFUSE_URL", "https://langfuse.test/");
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    const panel = await drawer();
    expect(within(panel).queryByText(/Kandidaten/)).toBeNull();
    fireEvent.click(within(panel).getByRole("button", { name: "Technische Details" }));
    expect(within(panel).getByText("0.90")).toBeInTheDocument();
    expect(within(panel).getByText(/0\.85/)).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: /Langfuse/ })).toHaveAttribute(
      "href",
      "https://langfuse.test/trace/trace-1",
    );
    vi.unstubAllEnvs();
  });

  it("retries a failed clause", async () => {
    renderWorkspace();
    act(() => useReviewUi.getState().select(4));
    const panel = await drawer();
    fireEvent.click(within(panel).getByRole("button", { name: "Technische Details" }));
    fireEvent.click(within(panel).getByRole("button", { name: "Erneut prüfen" }));
    await waitFor(() => expect(retries).toEqual([4]));
  });

  it("shows per-step votes only with the developer setting", async () => {
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    let panel = await drawer();
    fireEvent.click(within(panel).getByRole("button", { name: "Technische Details" }));
    expect(within(panel).queryByTestId("step-votes")).toBeNull();

    cleanup();
    localStorage.setItem("agb.dev", "1");
    useReviewUi.getState().reset();
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    panel = await drawer();
    fireEvent.click(within(panel).getByRole("button", { name: "Technische Details" }));
    fireEvent.click(within(panel).getByRole("button", { name: /„Urteil“/ }));
    await waitFor(() =>
      expect(posts).toEqual([
        { review_id: "r1", position: 0, vote: "down", reasons: ["verdict_wrong"] },
      ]),
    );
  });
});

describe("feedback payloads", () => {
  async function openDownForm() {
    act(() => useReviewUi.getState().select(0));
    const panel = await drawer();
    const control = within(panel).getByTestId("feedback-0");
    fireEvent.click(within(control).getByRole("button", { name: "Nicht hilfreich" }));
    return { panel, control };
  }

  it("posts a thumbs up", async () => {
    renderWorkspace();
    act(() => useReviewUi.getState().select(0));
    const control = within(await drawer()).getByTestId("feedback-0");
    fireEvent.click(within(control).getByRole("button", { name: "Hilfreich" }));
    await waitFor(() => expect(posts).toEqual([{ review_id: "r1", position: 0, vote: "up" }]));
  });

  it("verdict_wrong posts the corrected verdict", async () => {
    renderWorkspace();
    const { control } = await openDownForm();
    fireEvent.click(within(control).getByLabelText("Urteil falsch"));
    expect(within(control).getByRole("button", { name: "Senden" })).toBeDisabled();
    fireEvent.change(within(control).getByLabelText("Richtiges Urteil"), {
      target: { value: "valid" },
    });
    fireEvent.click(within(control).getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(posts).toEqual([
        {
          review_id: "r1",
          position: 0,
          vote: "down",
          reasons: ["verdict_wrong"],
          corrected_verdict: "valid",
        },
      ]),
    );
  });

  it("basis_wrong posts the corrected § list", async () => {
    renderWorkspace();
    const { control } = await openDownForm();
    fireEvent.click(within(control).getByLabelText("§ falsch/fehlt"));
    fireEvent.change(within(control).getByLabelText("Richtiger oder fehlender §"), {
      target: { value: "§ 309 Nr. 7 BGB, § 307 BGB" },
    });
    fireEvent.click(within(control).getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(posts).toEqual([
        {
          review_id: "r1",
          position: 0,
          vote: "down",
          reasons: ["basis_wrong"],
          corrected_basis: ["§ 309 Nr. 7 BGB", "§ 307 BGB"],
        },
      ]),
    );
  });

  it("reasoning_unclear and clause_cut post just their reason, with a comment", async () => {
    renderWorkspace();
    const { control } = await openDownForm();
    fireEvent.click(within(control).getByLabelText("Begründung unklar"));
    fireEvent.click(within(control).getByLabelText("Klausel falsch geschnitten"));
    fireEvent.change(within(control).getByLabelText("Kommentar (optional)"), {
      target: { value: " zu knapp " },
    });
    fireEvent.click(within(control).getByRole("button", { name: "Senden" }));
    await waitFor(() =>
      expect(posts).toEqual([
        {
          review_id: "r1",
          position: 0,
          vote: "down",
          reasons: ["reasoning_unclear", "clause_cut"],
          comment: "zu knapp",
        },
      ]),
    );
  });

  it("the card carries the same controls", async () => {
    renderWorkspace();
    const control = within(await card(1)).getByTestId("feedback-1");
    fireEvent.click(within(control).getByRole("button", { name: "Hilfreich" }));
    await waitFor(() => expect(posts).toEqual([{ review_id: "r1", position: 1, vote: "up" }]));
  });

  it("changes a vote from up to down and updates the buttons", async () => {
    renderWorkspace();
    const control = within(await card(1)).getByTestId("feedback-1");
    fireEvent.click(within(control).getByRole("button", { name: "Hilfreich" }));
    await waitFor(() =>
      expect(within(control).getByRole("button", { name: "Hilfreich" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );

    fireEvent.click(within(control).getByRole("button", { name: "Nicht hilfreich" }));
    fireEvent.click(within(control).getByLabelText("Klausel falsch geschnitten"));
    fireEvent.click(within(control).getByRole("button", { name: "Senden" }));

    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({
      review_id: "r1",
      position: 1,
      vote: "down",
      reasons: ["clause_cut"],
    });
    await waitFor(() =>
      expect(within(control).getByRole("button", { name: "Nicht hilfreich" })).toHaveAttribute(
        "aria-pressed",
        "true",
      ),
    );
    expect(within(control).getByRole("button", { name: "Hilfreich" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("rolls the optimistic vote back when the post fails", async () => {
    server.use(
      http.post("http://localhost/api/v1/feedback", () =>
        HttpResponse.json({ code: "boom", message: "boom" }, { status: 500 }),
      ),
    );
    renderWorkspace();
    const control = within(await card(1)).getByTestId("feedback-1");
    fireEvent.click(within(control).getByRole("button", { name: "Hilfreich" }));
    expect(await within(control).findByRole("alert")).toBeInTheDocument();
    expect(within(control).getByRole("button", { name: "Hilfreich" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});

describe("stored votes on reload", () => {
  it("shows the stored vote, and reopening a 👎 starts from what was sent", async () => {
    stored[0] = {
      ...stored[0]!,
      feedback: {
        feedback_id: "f0",
        agent_version: "v1",
        review_id: "r1",
        position: 0,
        vote: "down",
        reasons: ["basis_wrong"],
        corrected_basis: ["§ 309 BGB"],
        comment: "fehlt",
      } as Feedback,
    } as Finding;
    stored[1] = {
      ...stored[1]!,
      feedback: {
        vote: "up",
        position: 1,
        review_id: "r1",
        feedback_id: "f1",
        agent_version: "v1",
      } as Feedback,
    } as Finding;
    renderWorkspace();

    const down = within(await card(0)).getByRole("button", { name: "Nicht hilfreich" });
    expect(down).toHaveAttribute("aria-pressed", "true");
    expect(within(await card(1)).getByRole("button", { name: "Hilfreich" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(posts).toEqual([]);

    fireEvent.click(down);
    const control = screen.getByTestId("feedback-0");
    expect(within(control).getByLabelText("§ falsch/fehlt")).toBeChecked();
    expect(within(control).getByLabelText("Richtiger oder fehlender §")).toHaveValue("§ 309 BGB");
    expect(within(control).getByLabelText("Kommentar (optional)")).toHaveValue("fehlt");
  });
});

describe("activity feed", () => {
  it("logs status changes after the first snapshot only", async () => {
    stored = [finding(0, { status: "queued", verdict: null }), finding(1)];
    const { queryClient } = renderWorkspace();
    await card(0);
    fireEvent.click(screen.getByRole("button", { name: "Aktivität" }));
    expect(screen.getByText("Noch keine Änderungen seit dem Öffnen.")).toBeInTheDocument();

    stored = [finding(0, { status: "checking", verdict: null }), finding(1)];
    await act(() => queryClient.invalidateQueries());
    expect(await screen.findByText("1.1 · Klausel 0 — wird geprüft")).toBeInTheDocument();

    stored = [fixture()[0]!, finding(1)];
    await act(() => queryClient.invalidateQueries());
    expect(await screen.findByText("1.1 · Klausel 0 — geprüft")).toBeInTheDocument();
  });
});

import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "../../test/server";
import { HomePage } from "./home-page";
import { ORIGIN, documentRecord, renderPage, review, installOriginFetch } from "./test-utils";

installOriginFetch();

describe("HomePage", () => {
  it("shows an empty state with a link to the upload", async () => {
    server.use(http.get(`${ORIGIN}/api/v1/documents`, () => HttpResponse.json({ items: [] })));
    renderPage(<HomePage />);

    expect(await screen.findByText("Noch keine Prüfung")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: /Neue Prüfung/ })[0]!);
    expect(await screen.findByTestId("location")).toHaveTextContent("/upload");
  });

  it("lists documents with risk counts and progress", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ items: [{ document: documentRecord(), latest_review: review() }] }),
      ),
    );
    renderPage(<HomePage />);

    expect(await screen.findByText("vertrag.pdf")).toBeInTheDocument();
    expect(screen.getByText("Potenziell unwirksam")).toBeInTheDocument();
    expect(screen.getByText("Zu prüfen")).toBeInTheDocument();
    expect(screen.getByText("Wirksam")).toBeInTheDocument();
    expect(screen.getByText("Abgeschlossen")).toBeInTheDocument();
    expect(screen.getByText("10 von 10 Klauseln geprüft")).toBeInTheDocument();
    expect(screen.getByText("10.11.2026")).toBeInTheDocument();
  });

  it("starts an unreviewed document after showing the AI-cost scope dialog", async () => {
    let body: unknown;
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ items: [{ document: documentRecord(), latest_review: null }] }),
      ),
      http.post(`${ORIGIN}/api/v1/reviews`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ review: review({ review_id: "rev-new", status: "queued" }) });
      }),
    );
    renderPage(<HomePage />);

    fireEvent.click(await screen.findByRole("button", { name: "Prüfung starten" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("kann Kosten verursachen");
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Prüfung starten" }),
    );

    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-new");
    expect(body).toEqual({ document_id: "doc-1", scope: ["liability", "warranty"] });
  });

  it("traps focus in the start dialog and returns it after Escape", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ items: [{ document: documentRecord(), latest_review: null }] }),
      ),
    );
    renderPage(<HomePage />);

    const trigger = await screen.findByRole("button", { name: "Prüfung starten" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");

    expect(within(dialog).getByRole("button", { name: "Prüfung starten" })).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("keeps cancel and close disabled while a review starts", async () => {
    let resolveStart: ((response: Response) => void) | undefined;
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ items: [{ document: documentRecord(), latest_review: null }] }),
      ),
      http.post(
        `${ORIGIN}/api/v1/reviews`,
        () =>
          new Promise<Response>((resolve) => {
            resolveStart = resolve;
          }),
      ),
    );
    renderPage(<HomePage />);

    fireEvent.click(await screen.findByRole("button", { name: "Prüfung starten" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Prüfung starten" }));

    expect(within(dialog).getByRole("button", { name: "Abbrechen" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Dialog schließen" })).toBeDisabled();
    await waitFor(() => expect(resolveStart).toBeTypeOf("function"));
    resolveStart?.(new Response(JSON.stringify({ review: review({ review_id: "rev-new" }) })));
    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-new");
  });

  it("reopens a finished review without starting a new one", async () => {
    const starts: string[] = [];
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ items: [{ document: documentRecord(), latest_review: review() }] }),
      ),
      http.post(`${ORIGIN}/api/v1/reviews`, ({ request }) => {
        starts.push(request.url);
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderPage(<HomePage />);

    fireEvent.click(await screen.findByRole("button", { name: /vertrag\.pdf/ }));

    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-1");
    expect(starts).toEqual([]);
  });

  it("polls a running review every few seconds until it finishes", async () => {
    let polls = 0;
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({
          items: [
            {
              document: documentRecord(),
              latest_review: review({
                status: "running",
                counts: { total: 4, done: 1, failed: 0 },
              }),
            },
          ],
        }),
      ),
      http.get(`${ORIGIN}/api/v1/reviews/rev-1`, () => {
        polls += 1;
        return HttpResponse.json({
          review:
            polls < 2
              ? review({ status: "running", counts: { total: 4, done: 2, failed: 0 } })
              : review({ counts: { total: 4, done: 4, failed: 0, valid: 4 } }),
        });
      }),
    );
    renderPage(<HomePage />);

    expect(await screen.findByText("Läuft")).toBeInTheDocument();
    expect(await screen.findByText("Abgeschlossen", {}, { timeout: 8000 })).toBeInTheDocument();
    await waitFor(() => expect(polls).toBe(2));
    expect(screen.getByText("4 von 4 Klauseln geprüft")).toBeInTheDocument();
  }, 12000);

  it("shows an error when the list fails", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ code: "x", message: "x" }, { status: 500 }),
      ),
    );
    renderPage(<HomePage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("konnten nicht geladen werden");
  });

  it("shows decision progress per review and the lawyer agreement", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({
          items: [
            {
              document: documentRecord(),
              latest_review: review({
                status: "awaiting_decisions",
                counts: {
                  total: 10,
                  done: 10,
                  failed: 0,
                  potentially_void: 2,
                  uncertain: 3,
                  valid: 5,
                  decided: 4,
                  open_void: 1,
                  open_review: 2,
                },
              }),
            },
          ],
        }),
      ),
      http.get(`${ORIGIN}/api/v1/metrics/agreement`, () =>
        HttpResponse.json({
          agreement: {
            agent_version: "v1",
            decided: 12,
            kept: 8,
            overridden: 3,
            dismissed: 1,
            rate: 0.727,
          },
          current: true,
        }),
      ),
    );
    renderPage(<HomePage />);

    const row = await screen.findByTestId("home-decision-progress");
    expect(within(row).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4");
    expect(row).toHaveTextContent("4 von 10 entschieden");
    expect(screen.getByText("Entscheidungen offen")).toBeInTheDocument();

    const agreement = await screen.findByTestId("agreement");
    expect(within(agreement).getByTestId("agreement-rate")).toHaveTextContent("73 %");
    expect(agreement).toHaveTextContent("Entschieden: 12");
    expect(agreement).toHaveTextContent("Übernommen: 8");
    expect(agreement).toHaveTextContent("Überschrieben: 3");
    expect(agreement).toHaveTextContent("Verworfen: 1");
    expect(agreement).toHaveTextContent("Agent-Version v1");
  });

  it("says so when there are no decisions yet", async () => {
    server.use(
      http.get(`${ORIGIN}/api/v1/documents`, () => HttpResponse.json({ items: [] })),
      http.get(`${ORIGIN}/api/v1/metrics/agreement`, () =>
        HttpResponse.json({ agreement: { agent_version: "v1", decided: 0 }, current: true }),
      ),
    );
    renderPage(<HomePage />);

    expect(await screen.findByTestId("agreement")).toHaveTextContent("Noch keine Entscheidungen");
  });
});

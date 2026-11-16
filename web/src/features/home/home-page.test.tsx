import { fireEvent, screen, waitFor } from "@testing-library/react";
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
});

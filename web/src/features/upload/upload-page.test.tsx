import { fireEvent, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import de from "../../i18n/de.json";
import { server } from "../../test/server";
import {
  ORIGIN,
  documentOut,
  documentRecord,
  renderPage,
  review,
  installOriginFetch,
} from "../home/test-utils";
import { UploadPage } from "./upload-page";

installOriginFetch();

const CODES = [
  "too_large",
  "unsupported_type",
  "encrypted",
  "no_text_layer",
  "conversion_failed",
  "parse_failed",
  "parse_timeout",
  "empty",
  "archive_too_large",
  "rate_limited",
] as const;

function chooseFile() {
  const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
  const file = new File(["%PDF"], "vertrag.pdf", { type: "application/pdf" });
  fireEvent.change(input, { target: { files: [file] } });
}

function start() {
  fireEvent.click(screen.getByRole("button", { name: "Prüfung starten" }));
}

describe("UploadPage", () => {
  it("shows the disclaimer and both scopes checked by default", () => {
    renderPage(<UploadPage />, "/upload");
    expect(screen.getByText(de.app.disclaimer)).toBeInTheDocument();
    expect(screen.getByLabelText("Haftung")).toBeChecked();
    expect(screen.getByLabelText("Gewährleistung")).toBeChecked();
  });

  it.each(CODES)("shows a German message for %s", async (code) => {
    server.use(
      http.post(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json({ code, message: "raw" }, { status: 422 }),
      ),
    );
    renderPage(<UploadPage />, "/upload");
    chooseFile();
    start();

    expect(await screen.findByRole("alert")).toHaveTextContent(de.upload.errors[code]);
  });

  it("falls back to a generic message for an unknown failure", async () => {
    server.use(
      http.post(`${ORIGIN}/api/v1/documents`, () => new HttpResponse("<html>", { status: 502 })),
    );
    renderPage(<UploadPage />, "/upload");
    chooseFile();
    start();

    expect(await screen.findByRole("alert")).toHaveTextContent(de.upload.errors.generic);
  });

  it("rejects an empty submission and an empty scope locally", async () => {
    renderPage(<UploadPage />, "/upload");
    start();
    expect(await screen.findByRole("alert")).toHaveTextContent(de.upload.errors.empty);

    chooseFile();
    fireEvent.click(screen.getByLabelText("Haftung"));
    fireEvent.click(screen.getByLabelText("Gewährleistung"));
    start();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(de.upload.errors.no_scope),
    );
  });

  it("creates the document, starts a review with the chosen scope and opens it", async () => {
    let body: unknown;
    server.use(
      http.post(`${ORIGIN}/api/v1/documents`, () => HttpResponse.json(documentOut())),
      http.post(`${ORIGIN}/api/v1/reviews`, async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ review: review({ review_id: "rev-9", status: "queued" }) });
      }),
    );
    renderPage(<UploadPage />, "/upload");
    chooseFile();
    fireEvent.click(screen.getByLabelText("Gewährleistung"));
    start();

    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-9");
    expect(body).toEqual({ document_id: "doc-1", scope: ["liability"] });
  });

  it("accepts pasted text", async () => {
    let pasted: unknown;
    server.use(
      http.post(`${ORIGIN}/api/v1/documents`, async ({ request }) => {
        pasted = await request.json();
        return HttpResponse.json(documentOut());
      }),
      http.post(`${ORIGIN}/api/v1/reviews`, () =>
        HttpResponse.json({ review: review({ review_id: "rev-2" }) }),
      ),
    );
    renderPage(<UploadPage />, "/upload");
    fireEvent.click(screen.getByRole("button", { name: "Text einfügen" }));
    fireEvent.change(screen.getByLabelText("Vertragstext"), { target: { value: " § 1 Haftung " } });
    start();

    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-2");
    expect(pasted).toEqual({ text: "§ 1 Haftung" });
  });

  it("opens an already finished review without starting a new one", async () => {
    const starts: string[] = [];
    server.use(
      http.post(`${ORIGIN}/api/v1/documents`, () =>
        HttpResponse.json(
          documentOut({
            reused: true,
            document: documentRecord(),
            latest_review: review({ review_id: "rev-old" }),
          }),
        ),
      ),
      http.post(`${ORIGIN}/api/v1/reviews`, ({ request }) => {
        starts.push(request.url);
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderPage(<UploadPage />, "/upload");
    chooseFile();
    start();

    expect(await screen.findByTestId("location")).toHaveTextContent("/reviews/rev-old");
    expect(starts).toEqual([]);
  });
});

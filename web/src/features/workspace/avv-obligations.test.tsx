import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { afterEach } from "vitest";

import type { Finding, MissingObligation } from "../../api/types.gen";
import "../../i18n";
import { reviewData, finding } from "../viewer/fixtures";
import { useReviewUi } from "../review/store";
import { AvvObligations } from "./avv-obligations";

function avvFinding(position: number, code: "a" | "b", fulfilled: boolean): Finding {
  return {
    ...finding(position, "done", undefined, "valid"),
    obligation_findings: [
      {
        obligation: code,
        fulfilled,
        confidence: 0.9,
        legal_basis: ["Art. 28 Abs. 3 DSGVO"],
        reason_de: fulfilled ? "Die Pflicht ist geregelt." : "Die Regelung ist unvollständig.",
        reason_unavailable: false,
      },
    ],
  } as Finding;
}

const missing: MissingObligation = {
  review_id: "r1",
  obligation: {
    code: "c",
    label_de: "Art der personenbezogenen Daten",
    question_de: "Erfüllt die Klausel Art. 28 Abs. 3 lit. c?",
    legal_basis: "Art. 28 Abs. 3 lit. c DSGVO",
  },
  reason_de: "Keine Klausel deckt diese Pflicht ab.",
  finding_status: "open",
};

describe("AvvObligations", () => {
  afterEach(cleanup);

  it("shows clause findings and document-level missing obligations", () => {
    useReviewUi.getState().reset();
    const data = reviewData(
      [avvFinding(2, "a", true), avvFinding(4, "b", false)].map((item) => ({
        ...item,
        obligationFindings: item.obligation_findings,
      })) as never,
      { document_type: "avv" } as never,
      { missingObligations: [missing] },
    );
    render(<AvvObligations review={data} />);

    expect(screen.getByTestId("avv-obligations")).toHaveTextContent("AVV-Pflichten");
    expect(screen.getAllByText(/Art der personenbezogenen Daten/).length).toBeGreaterThan(0);
    expect(screen.getByText("Keine Klausel deckt diese Pflicht ab.")).toBeInTheDocument();
    expect(screen.getAllByText("a) Gegenstand und Dauer der Verarbeitung").length).toBeGreaterThan(
      0,
    );

    fireEvent.click(screen.getByRole("button", { name: "Offen" }));
    expect(screen.getByText(/Die Regelung ist unvollständig/)).toBeInTheDocument();
    expect(screen.queryByText("Die Pflicht ist geregelt.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Alle" }));
    fireEvent.change(screen.getByLabelText("Pflicht"), { target: { value: "a" } });
    expect(screen.getByText("Die Pflicht ist geregelt.")).toBeInTheDocument();
    expect(screen.queryByText(/Keine Klausel deckt/)).not.toBeInTheDocument();
  });

  it("selects the source clause from an AVV finding", () => {
    useReviewUi.getState().reset();
    const data = reviewData(
      [
        {
          ...avvFinding(2, "a", true),
          obligationFindings: avvFinding(2, "a", true).obligation_findings,
        },
      ] as never,
      { document_type: "avv" } as never,
    );
    render(<AvvObligations review={data} />);
    fireEvent.click(
      within(screen.getByTestId("avv-obligations")).getByRole("button", {
        name: "Klausel anzeigen",
      }),
    );
    expect(useReviewUi.getState().selected).toBe(2);
  });
});

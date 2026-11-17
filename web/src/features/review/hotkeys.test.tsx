import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import "../../i18n";
import { WorkspaceHotkeys } from "./hotkeys";
import { useReviewUi } from "./store";

beforeEach(() => {
  useReviewUi.getState().reset();
  useReviewUi.getState().setNavigable([2, 5, 9]);
});
afterEach(cleanup);

describe("workspace hotkeys", () => {
  it("steps through findings with J and K, and asks for the search with /", () => {
    render(
      <>
        <WorkspaceHotkeys />
        <input aria-label="Suche" />
      </>,
    );
    fireEvent.keyDown(document.body, { key: "j", code: "KeyJ" });
    expect(useReviewUi.getState().selected).toBe(2);
    fireEvent.keyDown(document.body, { key: "j", code: "KeyJ" });
    fireEvent.keyDown(document.body, { key: "k", code: "KeyK" });
    expect(useReviewUi.getState().selected).toBe(2);

    const before = useReviewUi.getState().focusSearch;
    fireEvent.keyDown(document.body, { key: "/", code: "Slash" });
    expect(useReviewUi.getState().focusSearch).toBe(before + 1);
  });

  it("ignores keystrokes typed into an input", () => {
    render(
      <>
        <WorkspaceHotkeys />
        <input aria-label="Suche" />
      </>,
    );
    fireEvent.keyDown(screen.getByLabelText("Suche"), { key: "j", code: "KeyJ" });
    expect(useReviewUi.getState().selected).toBeNull();
  });

  it("files a decision request for the selected finding with A, O and D", () => {
    render(<WorkspaceHotkeys />);
    for (const key of ["a", "o", "d"])
      fireEvent.keyDown(document.body, { key, code: `Key${key.toUpperCase()}` });
    // Nothing is selected, so nothing is requested.
    expect(useReviewUi.getState().decisionRequest).toBeNull();

    useReviewUi.getState().select(5);
    fireEvent.keyDown(document.body, { key: "o", code: "KeyO" });
    expect(useReviewUi.getState().decisionRequest).toMatchObject({
      position: 5,
      action: "override",
    });
    fireEvent.keyDown(document.body, { key: "d", code: "KeyD" });
    expect(useReviewUi.getState().decisionRequest).toMatchObject({
      position: 5,
      action: "dismiss",
    });
    fireEvent.keyDown(document.body, { key: "a", code: "KeyA" });
    expect(useReviewUi.getState().decisionRequest).toMatchObject({ position: 5, action: "accept" });
  });

  it("leaves A, O and D alone while a dialog is open", () => {
    useReviewUi.getState().select(5);
    render(
      <>
        <WorkspaceHotkeys />
        <div role="dialog" aria-label="offen" />
      </>,
    );
    fireEvent.keyDown(document.body, { key: "a", code: "KeyA" });
    expect(useReviewUi.getState().decisionRequest).toBeNull();
  });

  it("opens the shortcut overview with ? and lists the decision keys", () => {
    render(<WorkspaceHotkeys />);
    act(() => {
      fireEvent.keyDown(document.body, { key: "?", code: "Slash", shiftKey: true });
    });
    const dialog = screen.getByRole("dialog", { name: "Tastenkürzel" });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveTextContent("Fundstelle akzeptieren");
    expect(dialog).toHaveTextContent("Fundstelle verwerfen");
  });
});

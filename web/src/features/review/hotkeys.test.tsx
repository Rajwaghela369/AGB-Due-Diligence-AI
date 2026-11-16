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

  it("opens the shortcut overview with ? and binds nothing for A, O or D", () => {
    render(<WorkspaceHotkeys />);
    for (const key of ["a", "o", "d"])
      fireEvent.keyDown(document.body, { key, code: `Key${key.toUpperCase()}` });
    expect(useReviewUi.getState().selected).toBeNull();
    act(() => {
      fireEvent.keyDown(document.body, { key: "?", code: "Slash", shiftKey: true });
    });
    expect(screen.getByRole("dialog", { name: "Tastenkürzel" })).toBeVisible();
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.resetModules();
});

describe("useUiStore", () => {
  it("uses the saved theme before falling back to the OS preference and persists changes", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );
    localStorage.setItem("agb.theme", "light");
    const { useUiStore } = await import("./ui");

    expect(useUiStore.getState().dark).toBe(false);
    useUiStore.getState().toggleDark();
    expect(localStorage.getItem("agb.theme")).toBe("dark");
  });
});

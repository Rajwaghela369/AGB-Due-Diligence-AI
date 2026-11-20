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

  it("uses the OS preference when storage is unavailable", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: true })),
    );
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Storage is unavailable");
    });

    const { useUiStore } = await import("./ui");

    expect(useUiStore.getState().dark).toBe(true);
  });

  it("uses the OS preference when no saved choice exists", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({ matches: false })),
    );

    const { useUiStore } = await import("./ui");

    expect(useUiStore.getState().dark).toBe(false);
  });
});

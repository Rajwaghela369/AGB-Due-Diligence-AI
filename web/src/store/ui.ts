/** Small persistent UI preferences stay separate from server-owned review data. */

import { create } from "zustand";

interface UiState {
  dark: boolean;
  toggleDark: () => void;
}

const THEME_KEY = "agb.theme";
const THEME_COLORS = { dark: "#181a19", light: "#f7f6f3" } as const;

function initialDark(): boolean {
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "dark") return true;
    if (stored === "light") return false;
  } catch {
    // Storage can be unavailable in private browsing; the OS preference remains useful.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function saveTheme(dark: boolean): void {
  try {
    window.localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  } catch {
    // A preference is optional, so a storage failure must not prevent changing the theme.
  }
}

function applyTheme(dark: boolean): void {
  document.documentElement.classList.toggle("dark", dark);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? THEME_COLORS.dark : THEME_COLORS.light);
}

export const useUiStore = create<UiState>((set) => ({
  dark: initialDark(),
  toggleDark: () =>
    set((state) => {
      const dark = !state.dark;
      saveTheme(dark);
      applyTheme(dark);
      return { dark };
    }),
}));

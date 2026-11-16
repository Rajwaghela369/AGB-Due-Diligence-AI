/** Small persistent UI preferences stay separate from server-owned review data. */

import { create } from "zustand";

interface UiState {
  dark: boolean;
  toggleDark: () => void;
}

export const useUiStore = create<UiState>((set) => ({
  dark: false,
  toggleDark: () => set((state) => ({ dark: !state.dark })),
}));

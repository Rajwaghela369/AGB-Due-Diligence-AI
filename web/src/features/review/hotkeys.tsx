// Keyboard control of the review workspace: J/K step through findings, / jumps to the
// search, ? lists the shortcuts. react-hotkeys-hook ignores keystrokes made in inputs, so
// typing a "j" into the search box never moves the selection. Esc closes the drawer through
// Radix and clears the search where the search lives. A, O and D belong to Phase 7 and are
// deliberately unbound here.
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Keyboard, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { useTranslation } from "react-i18next";

import { useReviewUi } from "./store";

const SHORTCUTS = [
  { keys: ["J"], label: "hotkeys.next" },
  { keys: ["K"], label: "hotkeys.previous" },
  { keys: ["/"], label: "hotkeys.search" },
  { keys: ["Esc"], label: "hotkeys.escape" },
  { keys: ["?"], label: "hotkeys.help" },
] as const;

export function WorkspaceHotkeys() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const next = useReviewUi((s) => s.next);
  const previous = useReviewUi((s) => s.previous);
  const requestSearchFocus = useReviewUi((s) => s.requestSearchFocus);

  useHotkeys("j", next, { preventDefault: true }, [next]);
  useHotkeys("k", previous, { preventDefault: true }, [previous]);
  useHotkeys("slash", requestSearchFocus, { preventDefault: true }, [requestSearchFocus]);
  // "?" needs Shift on most layouts, so match the produced character, not a key code.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const typing =
        event.target instanceof Element &&
        event.target.closest("input, textarea, select, [contenteditable]") !== null;
      if (event.key !== "?" || typing || event.ctrlKey || event.metaKey || event.altKey) return;
      event.preventDefault();
      setOpen((value) => !value);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Trigger
        className="inline-flex h-8 items-center gap-2 rounded-md px-2 text-xs text-muted-foreground hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
        aria-label={t("hotkeys.open")}
      >
        <Keyboard aria-hidden="true" size={14} />
        {t("hotkeys.title")}
        <kbd className="rounded border border-border px-1 font-sans">?</kbd>
      </DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/30" />
        <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 w-[min(26rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-surface p-5 text-foreground shadow-lg focus-visible:outline-none">
          <div className="flex items-start justify-between gap-4">
            <DialogPrimitive.Title className="text-base font-semibold">
              {t("hotkeys.title")}
            </DialogPrimitive.Title>
            <DialogPrimitive.Close
              aria-label={t("hotkeys.close")}
              className="inline-flex size-8 items-center justify-center rounded-md hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
            >
              <X aria-hidden="true" size={16} />
            </DialogPrimitive.Close>
          </div>
          <DialogPrimitive.Description className="mt-1 text-sm text-muted-foreground">
            {t("hotkeys.description")}
          </DialogPrimitive.Description>
          <table className="mt-4 w-full text-sm">
            <thead className="sr-only">
              <tr>
                <th scope="col">{t("hotkeys.keys")}</th>
                <th scope="col">{t("hotkeys.action")}</th>
              </tr>
            </thead>
            <tbody>
              {SHORTCUTS.map((shortcut) => (
                <tr key={shortcut.label} className="border-t border-border">
                  <th scope="row" className="w-16 py-2 text-left font-normal">
                    {shortcut.keys.map((key) => (
                      <kbd
                        key={key}
                        className="rounded border border-border bg-muted px-1.5 py-0.5 font-sans text-xs"
                      >
                        {key}
                      </kbd>
                    ))}
                  </th>
                  <td className="py-2">{t(shortcut.label)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

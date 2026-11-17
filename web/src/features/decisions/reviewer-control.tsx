// The reviewer name: a chip with the current name (and „nicht angemeldet“) that opens the
// same dialog the first decision opens. `ReviewerDialogHost` is mounted once per workspace.
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { UserRound, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { MAX_NAME_LENGTH, useReviewer } from "./reviewer";

export function ReviewerControl() {
  const { t } = useTranslation();
  const name = useReviewer((s) => s.name);
  const edit = useReviewer((s) => s.edit);
  return (
    <button
      type="button"
      onClick={edit}
      data-testid="reviewer-control"
      aria-label={name ? t("reviewer.change", { name }) : t("reviewer.set")}
      className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs text-muted-foreground hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
    >
      <UserRound aria-hidden="true" size={14} />
      {name ? (
        <>
          <span className="max-w-[10rem] truncate text-foreground">{name}</span>
          <span>({t("reviewer.unauthenticated")})</span>
        </>
      ) : (
        t("reviewer.none")
      )}
    </button>
  );
}

function NameForm({ initial }: { initial: string }) {
  const { t } = useTranslation();
  const submit = useReviewer((s) => s.submit);
  const skip = useReviewer((s) => s.skip);
  const [value, setValue] = useState(initial);
  const empty = value.trim() === "";
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!empty) submit(value);
      }}
    >
      <label className="mt-4 block text-sm font-medium" htmlFor="reviewer-name">
        {t("reviewer.label")}
      </label>
      <input
        id="reviewer-name"
        autoFocus
        autoComplete="name"
        maxLength={MAX_NAME_LENGTH}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        className="mt-1 h-9 w-full rounded-md border border-border bg-surface px-2 text-sm"
      />
      <p className="mt-2 text-xs text-muted-foreground">{t("reviewer.hint")}</p>
      <div className="mt-5 flex flex-wrap justify-end gap-2">
        <button
          type="button"
          onClick={skip}
          className="rounded-md px-3 py-1.5 text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t("reviewer.skip")}
        </button>
        <button
          type="submit"
          disabled={empty}
          className="rounded-md bg-foreground px-3 py-1.5 text-sm text-background hover:opacity-85 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {t("reviewer.save")}
        </button>
      </div>
    </form>
  );
}

export function ReviewerDialogHost() {
  const { t } = useTranslation();
  const open = useReviewer((s) => s.dialogOpen);
  const name = useReviewer((s) => s.name);
  const cancel = useReviewer((s) => s.cancel);
  return (
    <DialogPrimitive.Root open={open} onOpenChange={(next) => !next && cancel()}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/30" />
        <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 w-[min(26rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-surface p-5 text-foreground shadow-lg focus-visible:outline-none">
          <div className="flex items-start justify-between gap-4">
            <DialogPrimitive.Title className="text-base font-semibold">
              {t("reviewer.title")}
            </DialogPrimitive.Title>
            <DialogPrimitive.Close
              aria-label={t("reviewer.close")}
              className="inline-flex size-8 items-center justify-center rounded-md hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
            >
              <X aria-hidden="true" size={16} />
            </DialogPrimitive.Close>
          </div>
          <DialogPrimitive.Description className="mt-1 text-sm text-muted-foreground">
            {t("reviewer.description")}
          </DialogPrimitive.Description>
          {open && <NameForm key="form" initial={name} />}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

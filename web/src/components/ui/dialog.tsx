/** Thin Radix Dialog wrapper; the finding drawer is its only user so far. */
import * as DialogPrimitive from "@radix-ui/react-dialog";
import type * as React from "react";

import { cn } from "../../lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTitle = DialogPrimitive.Title;
export const DialogDescription = DialogPrimitive.Description;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  className,
  ...props
}: React.ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Content
        className={cn(
          "fixed inset-y-0 right-0 z-40 flex w-full flex-col overflow-y-auto border-l border-border",
          "bg-surface p-5 shadow-lg focus-visible:outline-none sm:max-w-md lg:right-[25rem]",
          className,
        )}
        {...props}
      />
    </DialogPrimitive.Portal>
  );
}

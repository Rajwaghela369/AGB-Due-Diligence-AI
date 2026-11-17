import * as DialogPrimitive from "@radix-ui/react-dialog";
import { AlertCircle, Download, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, api, type ExportFormat } from "../../api/client";
import type { ExportBlocker } from "../../api/types.gen";
import { Button } from "../../components/ui/button";
import { useReviewer } from "../decisions/reviewer";
import { useReviewUi } from "../review/store";

function blockers(error: unknown): ExportBlocker[] | null {
  if (
    !(error instanceof ApiError) ||
    error.status !== 409 ||
    error.body.code !== "export_blocked"
  ) {
    return null;
  }
  const blocking = error.body.blocking;
  return Array.isArray(blocking) ? (blocking as ExportBlocker[]) : [];
}

export function ExportDialog({
  reviewId,
  defaultDraft,
  children,
}: {
  reviewId: string;
  defaultDraft: boolean;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [format, setFormat] = useState<ExportFormat>("docx");
  const [draft, setDraft] = useState(defaultDraft);
  const [pending, setPending] = useState(false);
  const [blocked, setBlocked] = useState<ExportBlocker[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reviewer = useReviewer((state) => state.name);
  const select = useReviewUi((state) => state.select);

  const download = async () => {
    setPending(true);
    setError(null);
    setBlocked(null);
    try {
      const file = await api.reviews.export(reviewId, format, { draft, reviewer });
      const url = URL.createObjectURL(file.blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      link.click();
      URL.revokeObjectURL(url);
      setOpen(false);
    } catch (reason) {
      const nextBlocked = blockers(reason);
      if (nextBlocked !== null) setBlocked(nextBlocked);
      else setError(reason instanceof Error ? reason.message : t("export.error"));
    } finally {
      setPending(false);
    }
  };

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Trigger asChild>
        <Button type="button">{children}</Button>
      </DialogPrimitive.Trigger>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-foreground/30" />
        <DialogPrimitive.Content className="fixed top-1/2 left-1/2 z-50 w-[min(32rem,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border bg-surface p-5 shadow-lg focus-visible:outline-none">
          <div className="flex items-start justify-between gap-3">
            <div>
              <DialogPrimitive.Title className="text-lg font-semibold">
                {t("export.title")}
              </DialogPrimitive.Title>
              <DialogPrimitive.Description className="mt-1 text-sm text-muted-foreground">
                {t("export.description")}
              </DialogPrimitive.Description>
            </div>
            <DialogPrimitive.Close
              aria-label={t("export.close")}
              className="rounded-md p-1 hover:bg-muted"
            >
              <X aria-hidden className="size-5" />
            </DialogPrimitive.Close>
          </div>
          <fieldset className="mt-5">
            <legend className="text-sm font-medium">{t("export.format")}</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {(["docx", "md", "csv"] as const).map((value) => (
                <label
                  key={value}
                  className="flex cursor-pointer items-center gap-1.5 rounded-md border border-border px-3 py-2 text-sm"
                >
                  <input
                    type="radio"
                    checked={format === value}
                    onChange={() => setFormat(value)}
                  />
                  {value.toUpperCase()}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="mt-4 flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft}
              onChange={(event) => setDraft(event.target.checked)}
            />
            <span>{t("export.draft")}</span>
          </label>
          {blocked !== null && (
            <div role="alert" className="mt-4 rounded-md border border-[var(--risk)] p-3 text-sm">
              <p className="flex items-center gap-2 font-medium">
                <AlertCircle aria-hidden className="size-4" />
                {t("export.blocked")}
              </p>
              <ul className="mt-2 list-inside list-disc">
                {blocked.map((finding) => (
                  <li key={finding.position}>
                    <button
                      type="button"
                      className="underline"
                      onClick={() => {
                        select(finding.position);
                        setOpen(false);
                      }}
                    >
                      {finding.number ?? t("summary.unnumbered")}
                      {finding.heading ? ` · ${finding.heading}` : ""}
                    </button>
                  </li>
                ))}
              </ul>
              <button type="button" className="mt-2 underline" onClick={() => setDraft(true)}>
                {t("export.useDraft")}
              </button>
            </div>
          )}
          {error && (
            <p role="alert" className="mt-4 text-sm">
              {error}
            </p>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <DialogPrimitive.Close asChild>
              <Button type="button" variant="ghost" disabled={pending}>
                {t("export.cancel")}
              </Button>
            </DialogPrimitive.Close>
            <Button type="button" onClick={() => void download()} disabled={pending}>
              <Download aria-hidden className="size-4" />
              {pending ? t("export.exporting") : t("export.action")}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}

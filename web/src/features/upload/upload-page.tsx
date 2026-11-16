/** Upload deliberately keeps validation feedback close to the action that caused it. */
import { FileUp, Loader2, Upload, XCircle } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiError, api } from "../../api/client";
import { Button } from "../../components/ui/button";

type UploadError =
  | "too_large"
  | "unsupported_type"
  | "encrypted"
  | "no_text_layer"
  | "conversion_failed"
  | "parse_failed"
  | "parse_timeout"
  | "empty"
  | "archive_too_large"
  | "rate_limited"
  | "no_scope"
  | "generic";

const ERROR_CODES = new Set<UploadError>([
  "too_large",
  "unsupported_type",
  "encrypted",
  "no_text_layer",
  "conversion_failed",
  "parse_failed",
  "parse_timeout",
  "empty",
  "archive_too_large",
  "rate_limited",
]);

export function UploadPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [pasteMode, setPasteMode] = useState(false);
  const [liability, setLiability] = useState(true);
  const [warranty, setWarranty] = useState(true);
  const [error, setError] = useState<UploadError | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const selectFile = (next: File | undefined) => {
    setFile(next ?? null);
    setPasteMode(false);
    setError(null);
  };
  const submit = async () => {
    if (!file && !text.trim()) {
      setError("empty");
      return;
    }
    if (!liability && !warranty) {
      setError("no_scope");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const document = await api.documents.create(file ?? { text: text.trim() });
      if (document.reused && document.latest_review?.status === "finished") {
        navigate(`/reviews/${document.latest_review.review_id}`, {
          state: { alreadyReviewed: true },
        });
        return;
      }
      const scope = [liability && "liability", warranty && "warranty"].filter(Boolean) as Array<
        "liability" | "warranty"
      >;
      const review = await api.reviews.start({ document_id: document.document.document_id, scope });
      navigate(`/reviews/${review.review.review_id}`);
    } catch (caught) {
      const code =
        caught instanceof ApiError && ERROR_CODES.has(caught.body.code as UploadError)
          ? (caught.body.code as UploadError)
          : "generic";
      setError(code);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section data-testid="upload-page" className="max-w-2xl space-y-7">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">{t("upload.title")}</h1>
        <p className="mt-2 text-muted-foreground">{t("upload.description")}</p>
      </header>
      {!pasteMode ? (
        <div
          className="rounded-xl border-2 border-dashed border-border bg-surface p-10 text-center"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            selectFile(event.dataTransfer.files.item(0) ?? undefined);
          }}
        >
          <FileUp aria-hidden="true" className="mx-auto mb-3 text-muted-foreground" size={32} />
          <p className="font-medium">{file ? file.name : t("upload.dropzone")}</p>
          <p className="mt-1 text-sm text-muted-foreground">{t("upload.formats")}</p>
          <input
            ref={input}
            className="sr-only"
            aria-label={t("upload.chooseFile")}
            tabIndex={-1}
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={(event) => selectFile(event.target.files?.[0])}
          />
          <Button
            className="mt-4"
            type="button"
            variant="ghost"
            onClick={() => input.current?.click()}
          >
            {t("upload.chooseFile")}
          </Button>
          <button
            className="mt-4 block w-full text-sm underline underline-offset-4"
            type="button"
            onClick={() => {
              setPasteMode(true);
              setFile(null);
              setError(null);
            }}
          >
            {t("upload.pasteAlternative")}
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          <label className="block font-medium" htmlFor="contract-text">
            {t("upload.pasteLabel")}
          </label>
          <textarea
            id="contract-text"
            className="min-h-56 w-full rounded-md border border-border bg-surface p-3 font-serif focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setError(null);
            }}
          />
          <button
            className="text-sm underline underline-offset-4"
            type="button"
            onClick={() => setPasteMode(false)}
          >
            {t("upload.fileAlternative")}
          </button>
        </div>
      )}
      <fieldset className="space-y-2 rounded-xl border border-border p-4">
        <legend className="px-1 font-medium">{t("upload.scope")}</legend>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={liability}
            onChange={(event) => setLiability(event.target.checked)}
          />
          {t("upload.liability")}
        </label>
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={warranty}
            onChange={(event) => setWarranty(event.target.checked)}
          />
          {t("upload.warranty")}
        </label>
      </fieldset>
      {error ? (
        <p
          role="alert"
          className="flex items-center gap-2 rounded-md border border-border p-3 text-sm"
        >
          <XCircle aria-hidden="true" size={18} />
          {t(`upload.errors.${error}`)}
        </p>
      ) : null}
      <Button disabled={submitting} type="button" onClick={() => void submit()}>
        {submitting ? (
          <Loader2 aria-hidden="true" className="animate-spin" size={18} />
        ) : (
          <Upload aria-hidden="true" size={18} />
        )}
        {t("upload.start")}
      </Button>
      <p className="text-sm text-muted-foreground">{t("app.disclaimer")}</p>
    </section>
  );
}

// The suggested wording against the original clause, word by word (Phase 8, 8.C). The server
// computes the diff (`ChatResult.diff`); this only renders it. Removed words are struck
// through, inserted words underlined, each on its own tint and announced to screen readers,
// so the change never depends on colour alone.
import { useTranslation } from "react-i18next";

import type { DiffSegment } from "../../api/types.gen";

export function DiffView({ diff }: { diff: DiffSegment[] }) {
  const { t } = useTranslation();
  return (
    <div className="space-y-1.5">
      <p className="font-contract whitespace-pre-line leading-relaxed" data-testid="chat-diff">
        {diff.map((segment, index) =>
          segment.op === "delete" ? (
            <del key={index} className="bg-risk-soft line-through decoration-risk">
              <span className="sr-only">{t("chat.removedSr")}</span>
              {segment.text}
            </del>
          ) : segment.op === "insert" ? (
            <ins key={index} className="bg-valid-soft underline decoration-valid">
              <span className="sr-only">{t("chat.insertedSr")}</span>
              {segment.text}
            </ins>
          ) : (
            <span key={index}>{segment.text}</span>
          ),
        )}
      </p>
      <p className="flex gap-3 text-xs text-muted-foreground" aria-hidden>
        <del className="bg-risk-soft line-through decoration-risk">{t("chat.legendRemoved")}</del>
        <ins className="bg-valid-soft underline decoration-valid">{t("chat.legendInserted")}</ins>
      </p>
    </div>
  );
}

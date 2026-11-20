// Pasted text, set in the contract serif, with the PDF's highlight model applied through
// character offsets: each clause is a block with the same margin bar, tint, marker and
// retry control as on a PDF page.
import { useEffect, useMemo, type ReactNode } from "react";

import { cn } from "../../lib/utils";
import type { ReviewFinding } from "../review/use-review";
import { ClauseMarker, RetryChip } from "./clause-parts";
import { emphasisTint, highlightStyle } from "./highlight";
import { barCss, pulseClass, type ClauseInteraction } from "./layout";
import { findMatches, splitByMatches, textBlocks } from "./text-segments";

export function TextView(props: {
  findings: ReviewFinding[];
  /** The stored source text; without it blocks are rebuilt from the clause texts. */
  source?: string | undefined;
  zoom: number;
  query: string;
  activeHit: number;
  interaction: ClauseInteraction;
  onHitCount: (count: number) => void;
}) {
  const { findings, interaction, onHitCount } = props;
  const byPosition = useMemo(() => new Map(findings.map((f) => [f.position, f])), [findings]);
  const { source } = props;
  const blocks = useMemo(
    () =>
      textBlocks(
        findings.map((f) => f.clause),
        source,
      ),
    [findings, source],
  );
  const needle = props.query.trim().length >= 2 ? props.query : "";
  const matches = useMemo(
    () => blocks.map((block) => (needle ? findMatches(block.text, needle) : [])),
    [blocks, needle],
  );
  const hitCount = matches.reduce((sum, list) => sum + list.length, 0);
  useEffect(() => onHitCount(hitCount), [hitCount, onHitCount]);

  let hitIndex = 0;
  const renderText = (text: string, ranges: Array<[number, number]>) =>
    splitByMatches(text, ranges).map((run, index) => {
      if (!run.match) return <span key={index}>{run.text}</span>;
      const global = hitIndex++;
      const active = global === props.activeHit;
      return (
        <mark
          key={index}
          data-hit-index={global}
          data-active={active || undefined}
          className="rounded-[2px] text-inherit"
          style={{
            background: `color-mix(in srgb, var(--accent) ${active ? 45 : 22}%, transparent)`,
            outline: active ? "2px solid var(--ring)" : undefined,
          }}
        >
          {run.text}
        </mark>
      );
    });

  return (
    <article
      className="mx-auto max-w-3xl bg-surface px-12 py-10 font-contract leading-relaxed shadow-sm ring-1 ring-border"
      style={{ fontSize: `${17 * props.zoom}px` }}
    >
      {blocks.map((block, index) => {
        const ranges = matches[index] ?? [];
        if (block.kind === "heading") {
          return (
            <h3 key={`h-${index}`} className="mt-6 mb-2 font-semibold first:mt-0">
              {renderText(block.text, ranges)}
            </h3>
          );
        }
        if (block.kind === "gap") {
          return (
            <p key={`g-${index}`} className="my-3 whitespace-pre-line">
              {renderText(block.text, ranges)}
            </p>
          );
        }
        const finding = byPosition.get(block.position);
        if (!finding) return null;
        return (
          <TextClause key={`c-${block.position}`} finding={finding} interaction={interaction}>
            {renderText(block.text, ranges)}
          </TextClause>
        );
      })}
    </article>
  );
}

function TextClause(props: {
  finding: ReviewFinding;
  interaction: ClauseInteraction;
  children: ReactNode;
}) {
  const { finding, interaction } = props;
  const style = highlightStyle(finding);
  const position = finding.position;
  if (!style.visible) {
    return (
      <p
        data-clause={position}
        data-clause-anchor={position}
        data-tone={style.tone}
        className="my-3 pl-8"
      >
        {props.children}
      </p>
    );
  }
  const emphasised = interaction.hovered === position || interaction.selected === position;
  const fill = emphasisTint(style, emphasised);
  return (
    <div
      data-clause={position}
      data-clause-anchor={position}
      data-tone={style.tone}
      data-emphasised={emphasised || undefined}
      className={cn(
        "relative my-3 flex gap-2 rounded-sm",
        interaction.selected === position && "outline-2 outline-offset-2 outline-ring",
      )}
    >
      <div className="flex w-6 shrink-0 flex-col items-center gap-1 pt-1">
        <ClauseMarker
          finding={finding}
          style={style}
          interaction={interaction}
          className="size-5"
        />
        <div
          aria-hidden="true"
          data-clause-bar={position}
          data-bar={style.bar}
          className="w-[3px] flex-1 rounded-full"
          style={barCss(style)}
        />
      </div>
      <div className="min-w-0 flex-1">
        <p
          className={cn(
            "cursor-pointer rounded-sm px-1 whitespace-pre-line",
            style.pulse && pulseClass,
          )}
          style={{ background: fill ?? undefined }}
          onMouseEnter={() => interaction.hover(position)}
          onMouseLeave={() => interaction.hover(null)}
          onClick={() => interaction.select(position)}
        >
          {props.children}
        </p>
        {style.retry && <RetryChip finding={finding} interaction={interaction} className="mt-1" />}
      </div>
    </div>
  );
}

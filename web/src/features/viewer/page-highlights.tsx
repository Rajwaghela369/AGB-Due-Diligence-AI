// The highlight overlay of one PDF page: per clause a tint on each of its lines, a margin
// bar spanning them, and on the clause's first page its state marker (and retry control
// when it failed). Boxes come from geometry.ts so they track zoom and device pixel ratio.
import type { CSSProperties } from "react";

import { cn } from "../../lib/utils";
import { ClauseMarker, RetryChip } from "./clause-parts";
import { pointsToCss, rectToCssBox, unionBox, type PageView, type PointBox } from "./geometry";
import { emphasisTint, highlightStyle } from "./highlight";
import { barCss, pulseClass, type ClauseInteraction, type PageClause } from "./layout";

const BAR_GAP = 8;
const BAR_WIDTH = 3;
const MARKER_SIZE = 20;

export function PageHighlights(props: {
  view: PageView;
  zoom: number;
  dpr: number;
  clauses: PageClause[];
  interaction: ClauseInteraction;
  hits: PointBox[];
  /** Global index of this page's first hit, and the active one. */
  hitOffset: number;
  activeHit: number;
}) {
  const { view, zoom, dpr, interaction } = props;
  return (
    <div className="pointer-events-none absolute inset-0" data-testid="page-highlights">
      {props.clauses.map((clause) => (
        <ClauseHighlight
          key={clause.finding.position}
          clause={clause}
          view={view}
          zoom={zoom}
          dpr={dpr}
          interaction={interaction}
        />
      ))}
      {props.hits.map((hit, index) => {
        const box = pointsToCss(hit, zoom, dpr);
        const global = props.hitOffset + index;
        const active = global === props.activeHit;
        return (
          <div
            key={`hit-${index}`}
            data-hit-index={global}
            data-active={active || undefined}
            className="absolute rounded-[2px] mix-blend-multiply dark:mix-blend-screen"
            style={{
              ...boxCss(box),
              background: `color-mix(in srgb, var(--accent) ${active ? 45 : 22}%, transparent)`,
              outline: active ? "2px solid var(--ring)" : undefined,
            }}
          />
        );
      })}
    </div>
  );
}

function ClauseHighlight(props: {
  clause: PageClause;
  view: PageView;
  zoom: number;
  dpr: number;
  interaction: ClauseInteraction;
}) {
  const { clause, view, zoom, dpr, interaction } = props;
  const { finding } = clause;
  const style = highlightStyle(finding);
  const boxes = clause.rects.map((rect) => rectToCssBox(rect, view, zoom, dpr));
  const span = unionBox(boxes);
  if (span === null) return null;

  const position = finding.position;
  if (!style.visible) {
    // Out of scope: nothing drawn, but the table can still scroll the document to it.
    return clause.first ? (
      <div
        aria-hidden="true"
        data-clause-anchor={position}
        className="absolute"
        style={boxCss(span)}
      />
    ) : null;
  }

  const emphasised = interaction.hovered === position || interaction.selected === position;
  const selected = interaction.selected === position;
  const fill = emphasisTint(style, emphasised);
  const barLeft = Math.max(1, span.left - BAR_GAP - BAR_WIDTH);
  const pointer = {
    onMouseEnter: () => interaction.hover(position),
    onMouseLeave: () => interaction.hover(null),
    onClick: () => interaction.select(position),
  };

  return (
    <div
      data-clause={position}
      data-tone={style.tone}
      data-emphasised={emphasised || undefined}
      className={cn(style.pulse && pulseClass)}
    >
      {boxes.map((box, index) => (
        <div
          key={index}
          data-clause-line={position}
          {...(clause.first && index === 0 ? { "data-clause-anchor": position } : {})}
          className="pointer-events-auto absolute cursor-pointer rounded-[2px]"
          style={{ ...boxCss(box), background: fill ?? undefined }}
          {...pointer}
        />
      ))}
      <div
        data-clause-bar={position}
        data-bar={style.bar}
        className="pointer-events-auto absolute cursor-pointer rounded-full"
        style={{
          left: barLeft,
          top: span.top,
          width: BAR_WIDTH,
          height: span.height,
          ...barCss(style),
        }}
        {...pointer}
      />
      {selected && (
        <div
          aria-hidden="true"
          className="absolute rounded-[3px]"
          style={{
            left: span.left - 2,
            top: span.top - 2,
            width: span.width + 4,
            height: span.height + 4,
            outline: "2px solid var(--ring)",
          }}
        />
      )}
      {clause.first && (
        <ClauseMarker
          finding={finding}
          style={style}
          interaction={interaction}
          className="pointer-events-auto absolute"
          css={{
            left: Math.max(0, barLeft - MARKER_SIZE - 2),
            top: span.top,
            width: MARKER_SIZE,
            height: MARKER_SIZE,
          }}
        />
      )}
      {clause.first && style.retry && (
        <RetryChip
          finding={finding}
          interaction={interaction}
          className="pointer-events-auto absolute right-2 -translate-y-full"
          css={{ top: Math.max(span.top - 2, 22) }}
        />
      )}
    </div>
  );
}

function boxCss(box: { left: number; top: number; width: number; height: number }): CSSProperties {
  return { left: box.left, top: box.top, width: box.width, height: box.height };
}

/** A virtual grid lets lawyers scan long contracts without rendering hundreds of verdict rows. */
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowDownUp, ArrowUp, CircleCheck, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import type { ReviewData, ReviewFinding } from "../review/use-review";
import { confidenceBand, riskStyle, toneOf } from "../review/risk";
import { useReviewUi } from "../review/store";

/** The same results as a sortable, filterable, virtualised grid (6.E). */
export function FindingsTable({ review }: { review: ReviewData }) {
  const { t } = useTranslation();
  const parent = useRef<HTMLDivElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const focusSearch = useReviewUi((state) => state.focusSearch);
  useEffect(() => {
    if (focusSearch > 0) searchInput.current?.focus();
  }, [focusSearch]);
  const [sorting, setSorting] = useState<SortingState>([]);
  const [search, setSearch] = useState("");
  const [verdict, setVerdict] = useState("all");
  const [topic, setTopic] = useState("all");
  const selected = useReviewUi((state) => state.selected);
  const hovered = useReviewUi((state) => state.hovered);
  const select = useReviewUi((state) => state.select);
  const hover = useReviewUi((state) => state.hover);
  const setView = useReviewUi((state) => state.setView);
  const topics = useMemo(
    () =>
      [
        ...new Set(
          review.findings
            .map((finding) => finding.clause.topic)
            .filter((value): value is string => Boolean(value)),
        ),
      ].sort(),
    [review.findings],
  );
  const columns = useMemo(() => makeColumns(t), [t]);
  // eslint-disable-next-line react-hooks/incompatible-library -- nothing downstream is memoised on the table
  const table = useReactTable({
    data: review.findings,
    columns,
    state: {
      sorting,
      globalFilter: search,
      columnFilters: [
        { id: "verdict", value: verdict },
        { id: "topic", value: topic },
      ],
    },
    sortDescFirst: false,
    onSortingChange: setSorting,
    onGlobalFilterChange: setSearch,
    globalFilterFn: (row, _, value) =>
      searchable(row.original).includes(String(value).toLocaleLowerCase()),
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });
  const rows = table.getRowModel().rows;
  const virtual = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parent.current,
    estimateSize: () => 58,
    overscan: 8,
  });
  // A selection made in the document or the panel should bring its row into view.
  const { scrollToIndex } = virtual;
  useEffect(() => {
    if (selected === null) return;
    const index = rows.findIndex((row) => row.original.position === selected);
    if (index >= 0) scrollToIndex(index);
  }, [selected, rows, scrollToIndex]);

  return (
    <section
      data-testid="findings-table"
      className="min-h-0 space-y-3"
      aria-label={t("table.label")}
    >
      <div className="flex flex-wrap gap-2">
        <label className="relative">
          <span className="sr-only">{t("table.search")}</span>
          <Search
            aria-hidden="true"
            className="absolute left-2 top-2.5 text-muted-foreground"
            size={16}
          />
          <input
            ref={searchInput}
            className="h-9 rounded-md border border-border bg-surface pl-8 pr-2 text-sm"
            placeholder={t("table.search")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label>
          <span className="sr-only">{t("table.verdict")}</span>
          <select
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={verdict}
            onChange={(event) => setVerdict(event.target.value)}
          >
            <option value="all">{t("table.allVerdicts")}</option>
            <option value="potentially_void">{t("risk.void")}</option>
            <option value="uncertain">{t("risk.review")}</option>
            <option value="valid">{t("risk.valid")}</option>
          </select>
        </label>
        <label>
          <span className="sr-only">{t("table.topic")}</span>
          <select
            className="h-9 rounded-md border border-border bg-surface px-2 text-sm"
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
          >
            <option value="all">{t("table.allTopics")}</option>
            {topics.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="overflow-x-auto rounded-xl border border-border bg-surface">
        <div className="min-w-[850px]" role="grid" aria-rowcount={rows.length + 1}>
          <div
            role="row"
            className="grid grid-cols-[4rem_minmax(16rem,2fr)_9rem_10rem_minmax(10rem,1fr)_7rem_9rem] border-b border-border bg-muted text-left text-sm font-medium"
          >
            {table.getHeaderGroups().map((group) =>
              group.headers.map((header) => (
                <div
                  key={header.id}
                  role="columnheader"
                  aria-sort={ariaSort(header.column.getIsSorted())}
                >
                  <button
                    type="button"
                    className="flex w-full items-center gap-1 px-3 py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={header.column.getToggleSortingHandler()}
                  >
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    <SortIcon direction={header.column.getIsSorted()} />
                  </button>
                </div>
              )),
            )}
          </div>
          <div ref={parent} className="h-[min(60vh,38rem)] overflow-auto" role="rowgroup">
            <div style={{ height: virtual.getTotalSize(), position: "relative" }}>
              {virtual.getVirtualItems().map((item) => {
                const row = rows[item.index];
                if (!row) return null;
                const active =
                  row.original.position === selected || row.original.position === hovered;
                return (
                  <button
                    key={row.id}
                    type="button"
                    role="row"
                    aria-selected={row.original.position === selected}
                    className={`grid w-full grid-cols-[4rem_minmax(16rem,2fr)_9rem_10rem_minmax(10rem,1fr)_7rem_9rem] border-b border-border text-left text-sm hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${active ? "bg-muted" : ""} ${row.original.position === selected ? "border-l-4 border-l-accent" : ""}`}
                    style={{
                      height: item.size,
                      position: "absolute",
                      top: 0,
                      transform: `translateY(${item.start}px)`,
                    }}
                    onClick={() => {
                      select(row.original.position);
                      setView("document");
                    }}
                    onMouseEnter={() => hover(row.original.position)}
                    onMouseLeave={() => hover(null)}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <span key={cell.id} role="gridcell" className="truncate px-3 py-3">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </span>
                    ))}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>
      <p className="text-sm text-muted-foreground">{t("table.rows", { count: rows.length })}</p>
    </section>
  );
}

function makeColumns(t: TFunction): ColumnDef<ReviewFinding>[] {
  return [
    { id: "position", accessorFn: (row) => row.position + 1, header: t("table.number") },
    {
      id: "clause",
      accessorFn: (row) => row.clause.text,
      header: t("table.clause"),
      cell: ({ row }) => <span className="font-serif">{row.original.clause.text}</span>,
    },
    {
      id: "topic",
      accessorFn: (row) => row.clause.topic ?? "",
      header: t("table.topic"),
      filterFn: (row, _, value) => value === "all" || row.original.clause.topic === value,
    },
    {
      id: "verdict",
      accessorFn: (row) => severity(row),
      sortingFn: "basic",
      header: t("table.verdict"),
      filterFn: (row, _, value) => value === "all" || row.original.verdict?.verdict === value,
      cell: ({ row }) => <Verdict finding={row.original} />,
    },
    {
      id: "basis",
      accessorFn: (row) => row.verdict?.legal_basis?.join(", ") ?? "",
      header: t("table.basis"),
      cell: ({ getValue }) => getValue<string>() || "—",
    },
    {
      id: "confidence",
      accessorFn: (row) => row.verdict?.confidence ?? 0,
      header: t("table.confidence"),
      cell: ({ row }) =>
        row.original.verdict
          ? t(`table.confidenceBands.${confidenceBand(row.original.verdict.confidence)}`)
          : "—",
    },
    {
      id: "status",
      accessorFn: (row) => row.status,
      header: t("table.status"),
      cell: ({ row }) => <Status finding={row.original} />,
    },
  ];
}

function Verdict({ finding }: { finding: ReviewFinding }) {
  const { t } = useTranslation();
  const style = riskStyle(finding);
  const Icon = style.icon;
  return (
    <span className="flex items-center gap-1" style={{ color: style.color }}>
      <Icon aria-hidden="true" size={16} />
      {t(style.label)}
    </span>
  );
}

function Status({ finding }: { finding: ReviewFinding }) {
  const { t } = useTranslation();
  if (finding.status !== "done") return <Verdict finding={finding} />;
  return (
    <span className="flex items-center gap-1">
      <CircleCheck aria-hidden="true" size={16} />
      {t("table.statusDone")}
    </span>
  );
}

/** Most serious first when sorted descending; unfinished clauses sort below real verdicts. */
function severity(finding: ReviewFinding): number {
  switch (toneOf(finding)) {
    case "void":
      return 3;
    case "review":
      return 2;
    case "valid":
      return 1;
    default:
      return 0;
  }
}

function ariaSort(direction: false | "asc" | "desc") {
  return direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none";
}

function SortIcon({ direction }: { direction: false | "asc" | "desc" }) {
  const Icon = direction === "asc" ? ArrowUp : direction === "desc" ? ArrowDown : ArrowDownUp;
  return <Icon aria-hidden="true" size={14} />;
}

function searchable(finding: ReviewFinding): string {
  return [
    finding.clause.text,
    finding.clause.topic,
    finding.verdict?.verdict,
    ...(finding.verdict?.legal_basis ?? []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLocaleLowerCase();
}

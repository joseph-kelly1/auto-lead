"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Parcel } from "@/lib/schema";
import { fmt, rampColor } from "@/lib/format";

const ROW = 28;
const OVERSCAN = 12;

export type SortKey = keyof Parcel;

const COLS: {
  key: SortKey;
  head: string;
  w: string;
  num?: boolean;
  render: (p: Parcel) => React.ReactNode;
}[] = [
  {
    key: "score",
    head: "Score",
    w: "62px",
    num: true,
    render: (p) => (
      <span className="inline-flex items-center gap-1.5">
        <i
          aria-hidden
          className="h-2 w-2 shrink-0 rounded-[1px]"
          style={{ background: rampColor(p.score) }}
        />
        {p.score.toFixed(3)}
      </span>
    ),
  },
  { key: "town", head: "Town", w: "88px", render: (p) => p.town },
  {
    key: "site_addr",
    head: "Address",
    w: "minmax(160px,1fr)",
    render: (p) => p.site_addr ?? "-",
  },
  {
    key: "margin",
    head: "Margin",
    w: "66px",
    num: true,
    render: (p) => fmt.pct(p.margin, 1),
  },
  {
    key: "profit",
    head: "Profit",
    w: "76px",
    num: true,
    render: (p) => fmt.usdK(p.profit),
  },
  { key: "arv", head: "Value", w: "76px", num: true, render: (p) => fmt.usdK(p.arv) },
  {
    key: "acq_price",
    head: "Acquire",
    w: "76px",
    num: true,
    render: (p) => fmt.usdK(p.acq_price),
  },
  {
    key: "rebuild_feasibility_score",
    head: "Feas",
    w: "52px",
    num: true,
    render: (p) => p.rebuild_feasibility_score.toFixed(2),
  },
  { key: "year_built", head: "Built", w: "56px", num: true, render: (p) => fmt.year(p.year_built) },
  { key: "res_area", head: "Living", w: "68px", num: true, render: (p) => fmt.num(p.res_area) },
  { key: "lot_acres", head: "Lot", w: "62px", num: true, render: (p) => p.lot_acres?.toFixed(2) ?? "-" },
  {
    key: "data_confidence_score",
    head: "Conf",
    w: "52px",
    num: true,
    render: (p) => fmt.pct(p.data_confidence_score),
  },
];

const GRID = COLS.map((c) => c.w).join(" ");

function Cells({ parcel }: { parcel: Parcel }) {
  return COLS.map((c) => (
    <span
      key={c.key}
      className={`truncate ${c.num ? "text-right" : ""} ${
        (c.key === "data_confidence_score" && parcel.data_confidence_score < 0.5) ||
        (c.key === "margin" && (parcel.margin ?? 0) <= 0) ||
        (c.key === "rebuild_feasibility_score" &&
          parcel.rebuild_feasibility_score === 0)
          ? "text-select"
          : ""
      }`}
      title={c.key === "site_addr" ? (parcel.site_addr ?? "") : undefined}
    >
      {c.render(parcel)}
    </span>
  ));
}

export default function ParcelTable({
  rows,
  loading,
  sort,
  desc,
  onSort,
  hovered,
  onHover,
  selected,
  onSelect,
  onReveal,
  revealRequest,
}: {
  rows: Parcel[];
  loading: boolean;
  sort: SortKey;
  desc: boolean;
  onSort: (k: SortKey) => void;
  hovered: string | null;
  onHover: (id: string | null) => void;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onReveal: (id: string) => void;
  revealRequest: { key: string; sequence: number } | null;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [top, setTop] = useState(0);
  const [height, setHeight] = useState(400);

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    setHeight(el.clientHeight);
    return () => ro.disconnect();
  }, []);

  // Only an explicit click reveals a map parcel in the scrolling list. Hover
  // is represented by the pinned preview row and never changes scroll position.
  useEffect(() => {
    if (!revealRequest || !box.current) return;
    const i = rows.findIndex((r) => r.row_key === revealRequest.key);
    if (i < 0) return;
    const el = box.current;
    const y = i * ROW;
    if (y < el.scrollTop || y + ROW > el.scrollTop + el.clientHeight)
      el.scrollTo({ top: Math.max(0, y - el.clientHeight / 2) });
  }, [revealRequest, rows]);

  const rowByKey = useMemo(
    () => new Map(rows.map((row) => [row.row_key, row])),
    [rows],
  );
  const hoveredParcel = hovered ? rowByKey.get(hovered) : undefined;

  const start = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const end = Math.min(rows.length, Math.ceil((top + height) / ROW) + OVERSCAN);
  const slice = rows.slice(start, end);

  return (
    /* Narrow screens scroll the table sideways rather than widening the
       document, which would otherwise push the detail panel off a phone. */
    <div className="flex min-h-0 flex-1 flex-col overflow-x-auto">
      <div
        className="grid min-w-[1040px] shrink-0 items-center gap-x-3 border-b border-line bg-panel px-3"
        style={{ gridTemplateColumns: GRID, height: ROW }}
        role="row"
      >
        {COLS.map((c) => (
          <button
            key={c.key}
            onClick={() => onSort(c.key)}
            className={`label truncate hover:text-text ${c.num ? "text-right" : "text-left"}`}
            aria-sort={sort === c.key ? (desc ? "descending" : "ascending") : "none"}
          >
            {c.head}
            {sort === c.key && (desc ? " ↓" : " ↑")}
          </button>
        ))}
      </div>

      <button
        type="button"
        disabled={!hoveredParcel}
        onClick={() => hoveredParcel && onReveal(hoveredParcel.row_key)}
        className={`tnum grid min-w-[1040px] shrink-0 items-center gap-x-3 border-b px-3 text-left ${
          hoveredParcel
            ? "border-select/40 bg-select/10 hover:bg-select/15"
            : "border-line bg-surface text-muted"
        }`}
        style={{ gridTemplateColumns: GRID, height: ROW }}
        aria-label={
          hoveredParcel
            ? `Reveal ${hoveredParcel.site_addr ?? "hovered parcel"} in the list`
            : undefined
        }
      >
        {hoveredParcel ? (
          <Cells parcel={hoveredParcel} />
        ) : (
          <span className="col-span-full text-[11px]">
            Hover over a parcel to preview it here
          </span>
        )}
      </button>

      <div ref={box} onScroll={(e) => setTop(e.currentTarget.scrollTop)} className="min-h-0 min-w-[1040px] flex-1 overflow-y-auto">
        {rows.length === 0 ? (
          <p className="p-6 text-muted">
            {loading
              ? "Loading parcels…"
              : "No parcels match. Widen a filter - \u201conly projects that pencil\u201d and minimum confidence are the usual culprits."}
          </p>
        ) : (
          <div style={{ height: rows.length * ROW, position: "relative" }}>
            <div style={{ transform: `translateY(${start * ROW}px)` }}>
              {slice.map((p) => {
                const on = p.row_key === selected;
                const hi = p.row_key === hovered;
                return (
                  <div
                    key={p.row_key}
                    role="row"
                    tabIndex={0}
                    onMouseEnter={() => onHover(p.row_key)}
                    onMouseLeave={() => onHover(null)}
                    onFocus={() => onHover(p.row_key)}
                    onClick={() => onSelect(on ? null : p.row_key)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onSelect(on ? null : p.row_key);
                      }
                    }}
                    className={`tnum grid cursor-default items-center gap-x-3 border-l-2 px-3 ${
                      on
                        ? "border-l-select bg-select/10"
                        : hi
                          ? "border-l-transparent bg-white/5"
                          : "border-l-transparent"
                    }`}
                    style={{ gridTemplateColumns: GRID, height: ROW }}
                  >
                    <Cells parcel={p} />
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

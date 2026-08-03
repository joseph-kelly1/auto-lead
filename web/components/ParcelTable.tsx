"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
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
    render: (p) => p.site_addr ?? "—",
  },
  { key: "year_built", head: "Built", w: "56px", num: true, render: (p) => fmt.num(p.year_built) },
  { key: "res_area", head: "Living", w: "68px", num: true, render: (p) => fmt.num(p.res_area) },
  { key: "lot_acres", head: "Lot", w: "62px", num: true, render: (p) => p.lot_acres?.toFixed(2) ?? "—" },
  {
    key: "val_land_ratio",
    head: "Land %",
    w: "62px",
    num: true,
    render: (p) => fmt.pct(p.val_land_ratio),
  },
  { key: "land_val", head: "Land", w: "76px", num: true, render: (p) => fmt.usdK(p.land_val) },
  {
    key: "score_coverage",
    head: "Cov",
    w: "52px",
    num: true,
    render: (p) => fmt.pct(p.score_coverage),
  },
];

const GRID = COLS.map((c) => c.w).join(" ");

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
  scrollTo,
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
  scrollTo: string | null;
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

  // Map hover scrolls the matching row into view, but only when it is off
  // screen — otherwise the list jitters as the pointer crosses parcels.
  useEffect(() => {
    if (!scrollTo || !box.current) return;
    const i = rows.findIndex((r) => r.row_key === scrollTo);
    if (i < 0) return;
    const el = box.current;
    const y = i * ROW;
    if (y < el.scrollTop || y + ROW > el.scrollTop + el.clientHeight)
      el.scrollTo({ top: Math.max(0, y - el.clientHeight / 2) });
  }, [scrollTo, rows]);

  const start = Math.max(0, Math.floor(top / ROW) - OVERSCAN);
  const end = Math.min(rows.length, Math.ceil((top + height) / ROW) + OVERSCAN);
  const slice = rows.slice(start, end);

  return (
    /* Narrow screens scroll the table sideways rather than widening the
       document, which would otherwise push the detail panel off a phone. */
    <div className="flex min-h-0 flex-1 flex-col overflow-x-auto">
      <div
        className="grid min-w-[760px] shrink-0 items-center gap-x-3 border-b border-line bg-panel px-3"
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

      <div ref={box} onScroll={(e) => setTop(e.currentTarget.scrollTop)} className="min-h-0 min-w-[760px] flex-1 overflow-y-auto">
        {rows.length === 0 ? (
          <p className="p-6 text-muted">
            {loading
              ? "Loading parcels…"
              : "No parcels match. Widen a filter — score coverage and year built are the usual culprits."}
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
                    {COLS.map((c) => (
                      <span
                        key={c.key}
                        className={`truncate ${c.num ? "text-right" : ""} ${
                          c.key === "score_coverage" && p.score_coverage < 0.7
                            ? "text-select"
                            : ""
                        }`}
                        title={c.key === "site_addr" ? (p.site_addr ?? "") : undefined}
                      >
                        {c.render(p)}
                      </span>
                    ))}
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

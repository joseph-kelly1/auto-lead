"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import FilterRail from "@/components/FilterRail";
import ParcelTable, { type SortKey } from "@/components/ParcelTable";
import DetailPanel from "@/components/DetailPanel";
import { ARTIFACT_BASE, query, queryOne } from "@/lib/duck";
import {
  DEFAULT_FILTERS,
  LIST_COLUMNS,
  type Filters,
  type Parcel,
  whereClause,
} from "@/lib/schema";
import { indexAnnotations, useAnnotations } from "@/lib/annotations";

// maplibre touches window at import time.
const ParcelMap = dynamic(() => import("@/components/ParcelMap"), { ssr: false });

export default function Page() {
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [rows, setRows] = useState<Parcel[]>([]);
  const [towns, setTowns] = useState<string[]>([]);
  const [count, setCount] = useState<number | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [sort, setSort] = useState<SortKey>("score");
  const [desc, setDesc] = useState(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [synthetic, setSynthetic] = useState(false);

  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [revealRequest, setRevealRequest] = useState<{
    key: string;
    sequence: number;
  } | null>(null);
  const revealSequence = useRef(0);
  const [flyTo, setFlyTo] = useState<{ lon: number; lat: number } | null>(null);

  const { store, update, replace } = useAnnotations();
  const annotated = useMemo(() => indexAnnotations(store), [store]);

  useEffect(() => {
    queryOne<{ synthetic: boolean }>(
      "SELECT coalesce(bool_and(release = 'synthetic-demo'), false) AS synthetic FROM parcels",
    )
      .then((r) => setSynthetic(r?.synthetic ?? false))
      .catch(() => {});
    query<{ town: string }>("SELECT DISTINCT town FROM parcels ORDER BY town")
      .then((r) => setTowns(r.map((x) => x.town)))
      .then(() => queryOne<{ n: number }>("SELECT count(*) AS n FROM parcels"))
      .then((r) => setTotal(r?.n ?? 0))
      .catch((e: unknown) =>
        setError(
          `Could not read parcels.parquet from ${ARTIFACT_BASE}. ` +
            `Restore the bundled demo, or set NEXT_PUBLIC_ARTIFACT_BASE to your artifacts. (${
              e instanceof Error ? e.message : String(e)
            })`,
        ),
      );
  }, []);

  const run = useCallback(async () => {
    setLoading(true);
    const where = whereClause(filters, annotated);
    try {
      const [c, r] = await Promise.all([
        queryOne<{ n: number }>(`SELECT count(*) AS n FROM parcels ${where}`),
        query<Parcel>(
          `SELECT ${LIST_COLUMNS} FROM parcels ${where}
           ORDER BY ${sort} ${desc ? "DESC" : "ASC"} NULLS LAST`,
        ),
      ]);
      setCount(c?.n ?? 0);
      setRows(r);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [filters, sort, desc, annotated]);

  // Filters are local queries, so there is no round trip to debounce away -
  // a frame of coalescing is enough to keep slider drags smooth.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(run, 60);
    return () => void (timer.current && clearTimeout(timer.current));
  }, [run]);

  // The tiles are keyed on loc_id; the table is keyed on loc_id|prop_id
  // because one polygon can carry two single-family records.
  const byLoc = useMemo(() => {
    const m = new Map<string, string>();
    for (const r of rows) if (!m.has(r.loc_id)) m.set(r.loc_id, r.row_key);
    return m;
  }, [rows]);
  const visibleIds = useMemo(() => new Set(rows.map((r) => r.loc_id)), [rows]);
  const scores = useMemo(
    () => new Map(rows.map((r) => [r.loc_id, r.score])),
    [rows],
  );

  const selectRow = useCallback(
    (key: string | null) => {
      setSelected(key);
      const r = rows.find((x) => x.row_key === key);
      if (r) setFlyTo({ lon: r.lon, lat: r.lat });
    },
    [rows],
  );

  const revealRow = useCallback(
    (key: string) => {
      selectRow(key);
      setRevealRequest({ key, sequence: ++revealSequence.current });
    },
    [selectRow],
  );

  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setSelected(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);

  const selectedLoc = selected?.split("|")[0] ?? null;
  const hoveredLoc = hovered?.split("|")[0] ?? null;

  return (
    <main className="flex h-full">
      <FilterRail
        filters={filters}
        setFilters={setFilters}
        towns={towns}
        count={count}
        total={total}
        loading={loading}
        store={store}
        replace={replace}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        {synthetic && (
          <p className="border-b border-line bg-panel px-4 py-2 text-muted">
            Synthetic demo. Parcels and financial values are fictional.
          </p>
        )}
        {error && (
          <div className="border-b border-select/40 bg-select/10 px-4 py-2 text-select">
            {error}
          </div>
        )}
        <div className="relative min-h-0 flex-[3]">
          <ParcelMap
            visibleIds={visibleIds}
            scores={scores}
            hovered={hoveredLoc}
            onHover={(loc) => {
              const key = loc ? (byLoc.get(loc) ?? null) : null;
              setHovered(key);
            }}
            selected={selectedLoc}
            onSelect={(loc) => {
              const key = loc ? (byLoc.get(loc) ?? null) : null;
              if (key) revealRow(key);
              else selectRow(null);
            }}
            flyTo={flyTo}
          />
        </div>
        <div className="flex min-h-0 flex-[2] flex-col border-t border-line">
          <ParcelTable
            rows={rows}
            loading={loading}
            sort={sort}
            desc={desc}
            onSort={(k) => {
              if (k === sort) setDesc(!desc);
              else {
                setSort(k);
                setDesc(true);
              }
            }}
            hovered={hovered}
            onHover={(id) => {
              setHovered(id);
            }}
            selected={selected}
            onSelect={selectRow}
            onReveal={revealRow}
            revealRequest={revealRequest}
          />
        </div>
      </div>

      {selected && (
        <DetailPanel
          rowKey={selected}
          annotation={store[selected]}
          onAnnotate={(patch) => update(selected, patch)}
          onClose={() => setSelected(null)}
        />
      )}
    </main>
  );
}

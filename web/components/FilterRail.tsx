"use client";

import { useRef } from "react";
import { ACRE_MAX, DEFAULT_FILTERS, YEAR_MAX, YEAR_MIN, type Filters } from "@/lib/schema";
import { STATUSES, type Store, exportFile, importFile } from "@/lib/annotations";
import { fmt } from "@/lib/format";

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="label">{label}</span>
        {hint && <span className="tnum text-[11px] text-muted">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

export default function FilterRail({
  filters,
  setFilters,
  towns,
  count,
  total,
  loading,
  store,
  replace,
}: {
  filters: Filters;
  setFilters: (f: Filters) => void;
  towns: string[];
  count: number | null;
  total: number | null;
  loading: boolean;
  store: Store;
  replace: (s: Store) => void;
}) {
  const file = useRef<HTMLInputElement>(null);
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) =>
    setFilters({ ...filters, [k]: v });

  return (
    <aside className="hidden h-full w-[248px] shrink-0 flex-col gap-7 overflow-y-auto border-r border-line bg-panel px-4 py-5 md:flex">
      <div>
        <h1 className="text-[15px] font-semibold tracking-tight">Teardown screener</h1>
        <p className="tnum mt-1 text-muted">
          {loading
            ? "Counting…"
            : count == null
              ? "—"
              : `${fmt.num(count)} of ${fmt.num(total)} parcels`}
        </p>
      </div>

      <Field label="Towns" hint={filters.towns.length ? `${filters.towns.length} on` : "all"}>
        <div className="space-y-1.5">
          {towns.map((t) => (
            <label key={t} className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={filters.towns.includes(t)}
                onChange={(e) =>
                  set(
                    "towns",
                    e.target.checked
                      ? [...filters.towns, t]
                      : filters.towns.filter((x) => x !== t),
                  )
                }
                className="accent-s3"
              />
              {t}
            </label>
          ))}
        </div>
      </Field>

      <Field label="Minimum score" hint={filters.minScore.toFixed(2)}>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={filters.minScore}
          onChange={(e) => set("minScore", +e.target.value)}
          className="w-full"
        />
      </Field>

      <Field label="Minimum data coverage" hint={fmt.pct(filters.minCoverage)}>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={filters.minCoverage}
          onChange={(e) => set("minCoverage", +e.target.value)}
          className="w-full"
        />
        <p className="text-[11px] leading-snug text-muted">
          Share of the scoring weight this parcel actually had data for. Below
          70% the score rests on a few signals.
        </p>
      </Field>

      <Field
        label="Year built"
        hint={`${filters.yearBuilt[0]}–${filters.yearBuilt[1]}`}
      >
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={YEAR_MIN}
            max={YEAR_MAX}
            value={filters.yearBuilt[0]}
            onChange={(e) => set("yearBuilt", [+e.target.value, filters.yearBuilt[1]])}
            className="tnum w-full rounded-sm border border-line bg-surface px-2 py-1"
            aria-label="Earliest year built"
          />
          <input
            type="number"
            min={YEAR_MIN}
            max={YEAR_MAX}
            value={filters.yearBuilt[1]}
            onChange={(e) => set("yearBuilt", [filters.yearBuilt[0], +e.target.value])}
            className="tnum w-full rounded-sm border border-line bg-surface px-2 py-1"
            aria-label="Latest year built"
          />
        </div>
      </Field>

      <Field label="Lot size" hint={`${filters.lotAcres[0]}–${filters.lotAcres[1]} ac`}>
        <div className="flex items-center gap-2">
          <input
            type="number"
            step={0.1}
            min={0}
            max={ACRE_MAX}
            value={filters.lotAcres[0]}
            onChange={(e) => set("lotAcres", [+e.target.value, filters.lotAcres[1]])}
            className="tnum w-full rounded-sm border border-line bg-surface px-2 py-1"
            aria-label="Smallest lot in acres"
          />
          <input
            type="number"
            step={0.1}
            min={0}
            max={ACRE_MAX}
            value={filters.lotAcres[1]}
            onChange={(e) => set("lotAcres", [filters.lotAcres[0], +e.target.value])}
            className="tnum w-full rounded-sm border border-line bg-surface px-2 py-1"
            aria-label="Largest lot in acres"
          />
        </div>
      </Field>

      <Field label="Minimum land share" hint={fmt.pct(filters.minLandRatio)}>
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={filters.minLandRatio}
          onChange={(e) => set("minLandRatio", +e.target.value)}
          className="w-full"
        />
      </Field>

      <Field label="Sewer">
        <div className="flex gap-1.5">
          {(["any", "sewer", "septic"] as const).map((v) => (
            <button
              key={v}
              onClick={() => set("sewer", v)}
              className={`flex-1 rounded-sm border py-1 capitalize ${
                filters.sewer === v
                  ? "border-select text-select"
                  : "border-line text-muted hover:border-muted"
              }`}
            >
              {v}
            </button>
          ))}
        </div>
        <label className="flex cursor-pointer items-center gap-2">
          <input
            type="checkbox"
            checked={filters.excludeFlood}
            onChange={(e) => set("excludeFlood", e.target.checked)}
            className="accent-s3"
          />
          Exclude FEMA flood zones
        </label>
      </Field>

      <Field label="Status">
        <div className="space-y-1.5">
          {STATUSES.map((s) => (
            <label key={s} className="flex cursor-pointer items-center gap-2 capitalize">
              <input
                type="checkbox"
                checked={filters.status.includes(s)}
                onChange={(e) =>
                  set(
                    "status",
                    e.target.checked
                      ? [...filters.status, s]
                      : filters.status.filter((x) => x !== s),
                  )
                }
                className="accent-s3"
              />
              {s}
            </label>
          ))}
          <label className="flex cursor-pointer items-center gap-2 pt-1">
            <input
              type="checkbox"
              checked={filters.hideDismissed}
              onChange={(e) => set("hideDismissed", e.target.checked)}
              className="accent-s3"
            />
            Hide dismissed
          </label>
        </div>
      </Field>

      <div className="mt-auto space-y-2 border-t border-line pt-4">
        <button
          onClick={() => setFilters(DEFAULT_FILTERS)}
          className="w-full rounded-sm border border-line py-1.5 hover:border-muted"
        >
          Reset filters
        </button>
        <div className="flex gap-2">
          <button
            onClick={() => exportFile(store)}
            className="flex-1 rounded-sm border border-line py-1.5 hover:border-muted"
          >
            Export notes
          </button>
          <button
            onClick={() => file.current?.click()}
            className="flex-1 rounded-sm border border-line py-1.5 hover:border-muted"
          >
            Import
          </button>
        </div>
        <input
          ref={file}
          type="file"
          accept="application/json"
          className="hidden"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            if (!f) return;
            try {
              replace(await importFile(f, store));
            } catch (err) {
              alert(
                `Could not import ${f.name}: ${
                  err instanceof Error ? err.message : "unreadable"
                }. Pick a file exported from this tool.`,
              );
            }
          }}
        />
        <p className="text-[11px] leading-snug text-muted">
          Notes live in this browser only. Export before switching machines.
        </p>
      </div>
    </aside>
  );
}

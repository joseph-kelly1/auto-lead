"use client";

import { useEffect, useState } from "react";
import { COMPONENTS } from "@/lib/schema";
import { query, sqlString } from "@/lib/duck";
import { fmt, fmtBy, rampColor } from "@/lib/format";
import { CHECKS, STATUSES, type Annotation, type Status } from "@/lib/annotations";

type Row = Record<string, number | string | boolean | null>;

function Bar({ pct }: { pct: number | null }) {
  if (pct == null)
    return <span className="text-[11px] text-muted">no data</span>;
  return (
    <span className="flex items-center gap-2">
      <span className="h-1.5 flex-1 rounded-[1px] bg-line">
        <span
          className="block h-full rounded-[1px]"
          style={{ width: `${Math.max(2, pct * 100)}%`, background: rampColor(pct) }}
        />
      </span>
      <span className="tnum w-9 shrink-0 text-right text-[11px]">
        {(pct * 100).toFixed(0)}
      </span>
    </span>
  );
}

/** Distance to the nearest DEP wetland, phrased by which rule it triggers. */
function wetland(m: number | null) {
  if (m == null) return "none within 250 m";
  if (m === 0) return "parcel overlaps a resource area";
  // 100 ft is the Wetlands Protection Act buffer zone, where the local
  // Conservation Commission has jurisdiction over the work.
  if (m <= 30.5) return `${m.toFixed(0)} m · inside the 100 ft buffer zone`;
  if (m <= 61) return `${m.toFixed(0)} m · near the 200 ft riverfront area`;
  return `${m.toFixed(0)} m away`;
}

function Stat({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="label">{k}</span>
      <span className="tnum text-right">{v}</span>
    </div>
  );
}

export default function DetailPanel({
  rowKey,
  annotation,
  onAnnotate,
  onClose,
}: {
  rowKey: string;
  annotation: Annotation | undefined;
  onAnnotate: (patch: Annotation) => void;
  onClose: () => void;
}) {
  const [row, setRow] = useState<Row | null>(null);
  const [note, setNote] = useState(annotation?.note ?? "");
  const [locId, propId] = rowKey.split("|");

  useEffect(() => {
    setRow(null);
    query<Row>(
      `SELECT * FROM parcels WHERE loc_id = ${sqlString(locId)}
         AND prop_id = ${sqlString(propId)} LIMIT 1`,
    ).then((r) => setRow(r[0] ?? null));
  }, [locId, propId]);

  useEffect(() => setNote(annotation?.note ?? ""), [rowKey, annotation?.note]);

  if (!row)
    return (
      <div className="fixed inset-0 z-50 border-l border-line bg-panel p-4 text-muted md:static md:w-[340px] md:shrink-0">
        Loading parcel…
      </div>
    );

  const n = (k: string) => (row[k] == null ? null : Number(row[k]));
  const s = (k: string) => (row[k] == null ? null : String(row[k]));
  const scope = "pct_"; // town percentile; nbr_ holds the neighbourhood one

  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-y-auto border-l border-line bg-panel md:static md:w-[340px] md:shrink-0">
      <header className="sticky top-0 z-10 border-b border-line bg-panel px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-[15px] font-semibold tracking-tight">
              {s("site_addr") ?? "Unaddressed parcel"}
            </h2>
            <p className="truncate text-muted">
              {s("town")} · {s("use_desc") ?? s("use_code")}
            </p>
          </div>
          <button onClick={onClose} aria-label="Close parcel" className="px-1 text-muted hover:text-text">
            ✕
          </button>
        </div>
        <div className="mt-3 flex items-baseline gap-3">
          <span className="tnum text-[22px] font-semibold leading-none" style={{ color: rampColor(n("score") ?? 0) }}>
            {(n("score") ?? 0).toFixed(3)}
          </span>
          <span className="text-muted">
            {fmt.pct(n("score_coverage"))} of the scoring weight had data
          </span>
        </div>
        {n("score_coverage")! < 0.7 && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            Thin score. {row.bldg_val_missing ? "The assessor reports no structure value for this parcel, so land share and value per square foot were excluded." : "Several components were unavailable."}
          </p>
        )}
      </header>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Why it ranked</p>
        <div className="space-y-2">
          {COMPONENTS.map((c) => (
            <div key={c.key}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[12px]">{c.label}</span>
                <span className="tnum text-[11px] text-muted">
                  {fmtBy(c.fmt, n(`val_${c.key}`))}
                </span>
              </div>
              <Bar pct={n(`${scope}${c.key}`)} />
            </div>
          ))}
          <div className="flex items-baseline justify-between gap-2 pt-1">
            <span className="text-[12px]">Absentee owner</span>
            <span className="tnum text-[11px] text-muted">{row.absentee ? "yes" : "no"}</span>
          </div>
        </div>
        <p className="mt-3 text-[11px] leading-snug text-muted">
          Bars are percentile within {s("town")}, 0–100. A 70% land share means
          something different here than in Fitchburg, so nothing is ranked
          statewide.
        </p>
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-1">Assessment · FY {n("fy")} · {s("release")}</p>
        <Stat k="Land" v={fmt.usd(n("land_val"))} />
        <Stat k="Structure" v={row.bldg_val_missing ? "not reported" : fmt.usd(n("bldg_val"))} />
        <Stat k="Total" v={fmt.usd(n("total_val"))} />
        <Stat k="Built" v={fmt.num(n("year_built"))} />
        <Stat k="Living area" v={`${fmt.num(n("res_area"))} sq ft`} />
        <Stat k="Lot (assessor)" v={fmt.acres(n("lot_acres"))} />
        <Stat k="Lot (mapped)" v={fmt.acres(n("gis_acres"))} />
        <Stat k="Zoning" v={s("zoning") ?? "not reported"} />
        <Stat
          k="Last sale"
          v={
            s("ls_date")
              ? `${s("ls_date")} · ${fmt.usd(n("ls_price"))}`
              : "none on record since 1950"
          }
        />
        <Stat k="Owner" v={<span className="font-normal">{s("owner1") ?? "—"}</span>} />
        {Number(row.n_records_on_parcel) > 1 && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            This polygon carries {String(row.n_records_on_parcel)} separate
            single-family records, so the mapped lot area is not attributable to
            one of them.
          </p>
        )}
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Site constraints</p>
        <Stat
          k="Sewer"
          v={
            row.flag_sewer == null
              ? "DEP has not mapped this town"
              : row.flag_sewer
                ? "in a municipal service area"
                : "outside any service area · assume septic"
          }
        />
        <Stat k="Wetland" v={wetland(n("flag_wetland_m"))} />
        <Stat k="FEMA flood" v={s("flag_flood") ?? "outside mapped SFHA"} />
        <Stat k="ACEC" v={s("flag_acec") ?? "none"} />
        <p className="mt-2 text-[11px] leading-snug text-muted">
          Screening only, from MassGIS layers. A delineation, a Title 5
          inspection and the town's own maps are the answers that count.
          {row.flag_sewer === false &&
            " Septic means a Title 5 inspection at transfer and $30–60k+ for a new system."}
        </p>
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Vetting</p>
        <div className="space-y-1.5">
          {CHECKS.map((c) => (
            <label key={c.key} className="flex cursor-pointer items-start gap-2 leading-snug">
              <input
                type="checkbox"
                checked={annotation?.checks?.[c.key] ?? false}
                onChange={(e) => onAnnotate({ checks: { [c.key]: e.target.checked } })}
                className="mt-0.5 accent-s3"
              />
              <span className="text-[12px]">{c.label}</span>
            </label>
          ))}
        </div>
        <p className="mt-2 text-[11px] leading-snug text-muted">
          None of these are in the assessor data. Demolition delay is the one
          that most often kills a MetroWest deal.
        </p>
      </section>

      <section className="space-y-3 px-4 py-3">
        <div>
          <p className="label mb-2">Status</p>
          <div className="flex flex-wrap gap-1.5">
            {STATUSES.map((st) => (
              <button
                key={st}
                onClick={() => onAnnotate({ status: annotation?.status === st ? undefined : st })}
                className={`rounded-sm border px-2 py-1 text-[12px] capitalize ${
                  annotation?.status === st
                    ? "border-select text-select"
                    : "border-line text-muted hover:border-muted"
                }`}
              >
                {st}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label mb-2 block" htmlFor="note">
            Notes
          </label>
          <textarea
            id="note"
            rows={4}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            onBlur={() => onAnnotate({ note })}
            placeholder="What you found. Saved when you click away."
            className="w-full resize-y rounded-sm border border-line bg-surface p-2 leading-snug placeholder:text-muted"
          />
        </div>
        <button
          onClick={() => onAnnotate({ dismissed: !annotation?.dismissed })}
          className="w-full rounded-sm border border-line py-1.5 hover:border-muted"
        >
          {annotation?.dismissed ? "Undismiss" : "Dismiss this parcel"}
        </button>
        <p className="font-mono text-[10px] leading-snug text-muted">
          {locId} · {propId}
        </p>
      </section>
    </div>
  );
}

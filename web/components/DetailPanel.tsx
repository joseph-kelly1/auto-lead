"use client";

import { useEffect, useState } from "react";
import {
  ACQ_COMPONENTS,
  COMPONENTS,
  GATES,
  PRO_FORMA,
  PROVENANCE,
  PROVENANCE_LABEL,
  SENSITIVITIES,
  SUBSCORES,
} from "@/lib/schema";
import { query, sqlString } from "@/lib/duck";
import { fmt, fmtBy, rampColor } from "@/lib/format";
import { CHECKS, STATUSES, type Annotation } from "@/lib/annotations";
import { loadMarketData, type MarketData } from "@/lib/market";

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

function Stat({ k, v, warn }: { k: string; v: React.ReactNode; warn?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="label">{k}</span>
      <span className={`tnum text-right ${warn ? "text-select" : ""}`}>{v}</span>
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
  const [market, setMarket] = useState<MarketData | null>(null);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [marketRevision, setMarketRevision] = useState(0);
  const [locId, propId] = rowKey.split("|");

  useEffect(() => {
    setRow(null);
    query<Row>(
      `SELECT * FROM parcels WHERE loc_id = ${sqlString(locId)}
         AND prop_id = ${sqlString(propId)} LIMIT 1`,
    ).then((r) => setRow(r[0] ?? null));
  }, [locId, propId]);

  useEffect(() => setNote(annotation?.note ?? ""), [rowKey, annotation?.note]);

  useEffect(() => {
    const changed = () => setMarketRevision((revision) => revision + 1);
    window.addEventListener("market-credentials-changed", changed);
    return () => window.removeEventListener("market-credentials-changed", changed);
  }, []);

  useEffect(() => {
    const address =
      typeof row?.site_addr === "string" ? row.site_addr.trim() : "";
    const town = typeof row?.town === "string" ? row.town.trim() : "";
    if (!address || !town) {
      setMarket(null);
      setMarketError(null);
      return;
    }

    const controller = new AbortController();
    setMarket(null);
    setMarketError(null);
    loadMarketData({
      address,
      town,
      lat: row?.lat == null ? null : Number(row.lat),
      lon: row?.lon == null ? null : Number(row.lon),
      signal: controller.signal,
    })
      .then(setMarket)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setMarketError(
            reason instanceof Error ? reason.message : "Market lookup failed.",
          );
      });
    return () => controller.abort();
  }, [row, marketRevision]);

  if (!row)
    return (
      <div className="fixed inset-0 z-50 border-l border-line bg-panel p-4 text-muted md:static md:w-[340px] md:shrink-0">
        Loading parcel…
      </div>
    );

  const n = (k: string) => (row[k] == null ? null : Number(row[k]));
  const s = (k: string) => (row[k] == null ? null : String(row[k]));
  const scope = "pct_"; // town percentile; nbr_ holds the neighbourhood one
  const address = s("site_addr");
  const town = s("town");
  const zillowSearchUrl =
    address && town
      ? `https://www.zillow.com/homes/${encodeURIComponent(`${address}, ${town}, MA`)}_rb/`
      : null;
  const zillowUrl = market?.zillow?.url ?? zillowSearchUrl;

  const blocked = n("rebuild_feasibility_score") === 0;
  const firedGates = GATES.filter((g) => row[`gate_${g.key}`] === true);
  const margin = n("margin");
  const confidence = n("data_confidence_score") ?? 0;
  const listPrice = n("list_price");

  return (
    <div className="fixed inset-0 z-50 flex flex-col overflow-y-auto border-l border-line bg-panel md:static md:w-[340px] md:shrink-0">
      <header className="sticky top-0 z-10 border-b border-line bg-panel px-4 py-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-[15px] font-semibold tracking-tight">
              {address ?? "Unaddressed parcel"}
            </h2>
            <p className="truncate text-muted">
              {town} · {s("use_desc") ?? s("use_code")}
            </p>
          </div>
          <button onClick={onClose} aria-label="Close parcel" className="px-1 text-muted hover:text-text">
            ✕
          </button>
        </div>
        <div className="mt-3 flex items-baseline gap-3">
          <span
            className="tnum text-[22px] font-semibold leading-none"
            style={{ color: rampColor(n("score") ?? 0) }}
          >
            {(n("score") ?? 0).toFixed(3)}
          </span>
          <span className="text-muted">
            rebuild opportunity · {fmt.pct(confidence)} confidence
          </span>
        </div>
        {blocked && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            Hard feasibility blocker. This parcel scores zero overall no matter
            how the economics look, because a replacement house cannot be built
            as things stand.
          </p>
        )}
        {!blocked && margin != null && margin <= 0 && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            Does not pencil at the configured costs: the estimated completed
            value does not cover acquisition plus construction.
          </p>
        )}
        {confidence < 0.5 && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            Thin data. Most of this score rests on estimates rather than
            authoritative inputs — see the provenance list below.
          </p>
        )}
        {zillowUrl && (
          <a
            href={zillowUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-3 inline-flex rounded-sm border border-line px-2 py-1 text-[12px] text-muted hover:border-muted hover:text-text"
            aria-label="View this property on Zillow (opens in a new tab)"
          >
            View on Zillow ↗
          </a>
        )}
      </header>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">How it scored</p>
        <div className="space-y-2">
          {SUBSCORES.map((c) => (
            <div key={c.key}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[12px]">{c.label}</span>
                <span className="tnum text-[11px] text-muted">
                  {n(c.key) == null ? "—" : (n(c.key) as number).toFixed(2)}
                </span>
              </div>
              <Bar pct={n(c.key)} />
              <p className="mt-0.5 text-[11px] leading-snug text-muted">{c.hint}</p>
            </div>
          ))}
        </div>
        <p className="mt-3 text-[11px] leading-snug text-muted">
          Feasibility × economics × confidence, then re-ordered a little by
          acquisition likelihood and obsolescence. The first three multiply, so
          any one of them being near zero settles the matter.
        </p>
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Project pro forma</p>
        {n("arv") == null ? (
          <p className="py-1 text-[12px] text-muted">
            No resale value: this town does not have enough recorded
            new-construction sales to build a comparable set. Nothing is
            estimated in its place.
          </p>
        ) : (
          <>
            <Stat
              k="Completed value"
              v={fmt.usd(n("arv"))}
            />
            <p className="mb-1 text-[11px] leading-snug text-muted">
              {fmt.num(n("buildable_sqft"))} sq ft replacement house, priced off{" "}
              {fmt.num(n("comp_count_local"))} local new-build sales at{" "}
              {fmt.usd(n("comp_ppsf_local") ?? n("comp_ppsf_town"))}/sq ft.
            </p>
            {PRO_FORMA.map((line) => (
              <Stat
                key={line.key}
                k={line.label}
                v={
                  line.key === "acq_price" && listPrice != null
                    ? `${fmt.usd(n(line.key))} · listed`
                    : fmt.usd(n(line.key))
                }
              />
            ))}
            <div className="mt-1 border-t border-line pt-1">
              <Stat k="Total cost" v={fmt.usd(n("total_cost"))} />
              <Stat
                k="Gross profit"
                v={fmt.usd(n("profit"))}
                warn={(n("profit") ?? 0) <= 0}
              />
              <Stat
                k="Margin on value"
                v={fmt.pct(margin, 1)}
                warn={(margin ?? 0) <= 0}
              />
              <Stat k="Return on cost" v={fmt.pct(n("return_on_cost"), 1)} />
            </div>
            <p className="label mb-1 mt-3">Sensitivity</p>
            {SENSITIVITIES.map((sn) => (
              <Stat
                key={sn.key}
                k={sn.label}
                v={fmt.pct(n(sn.key), 1)}
                warn={(n(sn.key) ?? 0) <= 0}
              />
            ))}
            {n("subdivision_lots") != null && (n("subdivision_lots") as number) >= 2 && (
              <p className="mt-2 text-[11px] leading-snug text-select">
                Lot is {fmt.ratio(n("subdivision_lots"))}× the zoning minimum, so
                a split may be possible. Scored as bounded upside only — this is
                not a two-lot pro forma, and a real subdivision needs frontage, a
                road and a planning board.
              </p>
            )}
            <p className="mt-2 text-[11px] leading-snug text-muted">
              Costs come from <code>config.toml</code> and are assumptions, not
              data. The completed value is modelled from recorded new-build sales
              and carries real error — treat it as a screen, not an appraisal.
            </p>
          </>
        )}
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Rebuild feasibility</p>
        {firedGates.length === 0 ? (
          <p className="py-1 text-[12px] text-muted">
            No constraint fired from the data we hold. That is not the same as a
            clear site: zoning dimensional limits are absent here, and MHC
            historic coverage is uneven.
          </p>
        ) : (
          <div className="space-y-2">
            {firedGates.map((g) => (
              <div key={g.key}>
                <p className="text-[12px] text-select">{g.label}</p>
                <p className="text-[11px] leading-snug text-muted">{g.note}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Data provenance</p>
        <div className="space-y-1">
          {PROVENANCE.map((p) => {
            const v = n(`prov_${p.key}`);
            const label = v == null ? "—" : PROVENANCE_LABEL[v];
            return (
              <div key={p.key} className="flex items-baseline justify-between gap-3">
                <span className="text-[12px]">{p.label}</span>
                <span
                  className={`text-[11px] ${
                    v === 2 ? "text-text" : v === 1 ? "text-muted" : "text-select"
                  }`}
                >
                  {label}
                </span>
              </div>
            );
          })}
        </div>
        <p className="mt-2 text-[11px] leading-snug text-muted">
          Missing inputs lower confidence. Nothing missing is ever filled in with
          a favourable guess.
        </p>
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-1">Assessment · FY {n("fy")} · {s("release")}</p>
        <Stat k="Land" v={fmt.usd(n("land_val"))} />
        <Stat k="Structure" v={row.bldg_val_missing ? "not reported" : fmt.usd(n("bldg_val"))} />
        <Stat k="Total" v={fmt.usd(n("total_val"))} />
        <Stat k="Built" v={fmt.year(n("year_built"))} />
        <Stat k="Living area" v={`${fmt.num(n("res_area"))} sq ft`} />
        <Stat k="Lot (assessor)" v={fmt.acres(n("lot_acres"))} />
        <Stat k="Lot (mapped)" v={fmt.acres(n("gis_acres"))} />
        <Stat k="Lot width (bbox)" v={`${fmt.num(n("lot_width_ft"))} ft`} />
        <Stat k="Zoning" v={s("zoning") ?? "not reported"} />
        <Stat
          k="Zoning minimum"
          v={
            n("min_lot_acres") == null
              ? "no authoritative table loaded"
              : fmt.acres(n("min_lot_acres"))
          }
        />
        <Stat
          k="Last sale"
          v={
            row.ls_date == null
              ? "none on record since 1950"
              : `${fmt.date(row.ls_date as string | number)} · ${fmt.usd(n("ls_price"))}`
          }
        />
        <Stat k="Owner" v={<span className="font-normal">{s("owner1") ?? "—"}</span>} />
        <Stat
          k="Motivation signals"
          v={
            [
              row.absentee ? "absentee" : null,
              row.estate ? "estate" : null,
              row.care_of ? "care-of address" : null,
            ]
              .filter(Boolean)
              .join(", ") || "none found"
          }
        />
        {row.bldg_val_missing === true && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            The assessor reports no structure value here, so the total assessment
            is depressed too. The acquisition estimate is taken from land value
            instead, and structure value counts as missing.
          </p>
        )}
        {Number(row.n_records_on_parcel) > 1 && (
          <p className="mt-2 text-[11px] leading-snug text-select">
            This polygon carries {String(row.n_records_on_parcel)} separate
            single-family records, so the mapped lot area is not attributable to
            one of them.
          </p>
        )}
      </section>

      <section className="border-b border-line px-4 py-3">
        <p className="label mb-2">Why the existing house is obsolete</p>
        <div className="space-y-2">
          {[...COMPONENTS, ...ACQ_COMPONENTS].map((c) => (
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
        </div>
        <p className="mt-3 text-[11px] leading-snug text-muted">
          Bars are percentile within {town}, 0–100. These were the entire score
          in an earlier version; they are now a supporting factor, because a
          worthless structure is evidence about the old house, not about whether
          a new one can be built or sold at a profit.
        </p>
      </section>

      {(market?.configured || marketError) && (
        <section className="border-b border-line px-4 py-3">
          <p className="label mb-1">Current market</p>
          {market?.mls ? (
            <>
              <Stat
                k="MLS status"
                v={`${market.mls.onMarket ? "On market" : "Off market"} · ${market.mls.status}`}
              />
              {market.mls.listPrice != null && (
                <Stat k="List price" v={fmt.usd(market.mls.listPrice)} />
              )}
              {market.mls.closePrice != null && (
                <Stat k="Close price" v={fmt.usd(market.mls.closePrice)} />
              )}
              {market.mls.listingId && (
                <Stat k="MLS ID" v={market.mls.listingId} />
              )}
            </>
          ) : market?.mlsConfigured ? (
            <p className="py-1 text-[12px] text-muted">
              No matching MLS listing returned.
            </p>
          ) : null}

          {market?.zillow ? (
            <>
              <Stat k="Zestimate" v={fmt.usd(market.zillow.zestimate)} />
              {(market.zillow.low != null || market.zillow.high != null) && (
                <Stat
                  k="Estimate range"
                  v={`${fmt.usd(market.zillow.low)}–${fmt.usd(market.zillow.high)}`}
                />
              )}
            </>
          ) : market?.zillowConfigured ? (
            <p className="py-1 text-[12px] text-muted">
              No Zestimate returned for this address.
            </p>
          ) : null}

          {market?.checkedAt && (
            <p className="mt-2 text-[11px] text-muted">
              Checked {market.checkedAt.slice(0, 10)} · cached for 24 hours
            </p>
          )}
          <p className="mt-2 text-[11px] leading-snug text-muted">
            Live lookup, shown for context. It does not feed the ranking: the
            score is computed offline and the app has to work without provider
            credentials. To let a real list price into the pro forma, export it
            to <code>pipeline/listings.csv</code> and rebuild.
          </p>
          {market?.errors?.map((error) => (
            <p key={error} className="mt-1 text-[11px] leading-snug text-select">
              {error}
            </p>
          ))}
          {marketError && (
            <p className="mt-1 text-[11px] leading-snug text-select">
              {marketError}
            </p>
          )}
        </section>
      )}

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
        <Stat k="MHC inventory" v={s("flag_historic") ?? "no record"} />
        <Stat k="MHC area" v={s("flag_historic_area") ?? "none"} />
        <p className="mt-2 text-[11px] leading-snug text-muted">
          Screening only, from MassGIS layers. A delineation, a Title 5
          inspection and the town's own maps are the answers that count. An
          absent MHC record is not evidence a house is unlisted — Newton has 155
          inventoried points against 17,000 single families.
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
          None of these are settled by the assessor data. Demolition delay is the
          one that most often kills a MetroWest deal.
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

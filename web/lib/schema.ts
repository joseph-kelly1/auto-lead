/** Shared shapes and the filter -> SQL translation. */

import { sqlString } from "./duck";

export type Parcel = {
  row_key: string;
  loc_id: string;
  prop_id: string;
  town: string;
  site_addr: string | null;
  owner1: string | null;
  score: number;
  score_coverage: number;
  year_built: number | null;
  res_area: number | null;
  lot_acres: number | null;
  gis_acres: number | null;
  land_val: number;
  bldg_val: number;
  total_val: number;
  val_land_ratio: number | null;
  ls_date: string | null;
  fy: number;
  release: string;
  absentee: boolean;
  bldg_val_missing: boolean;
  flag_sewer: boolean | null;
  flag_wetland_m: number | null;
  flag_flood: string | null;
  n_records_on_parcel: number;
  lon: number;
  lat: number;
};

/** The eight signals, in the order the detail panel shows them. */
export const COMPONENTS = [
  { key: "land_ratio", label: "Land share of value", fmt: "pct" },
  { key: "impr_psf", label: "Structure value per sq ft", fmt: "usd" },
  { key: "area_vs_nbhd", label: "Size vs neighbours", fmt: "ratio" },
  { key: "far", label: "Structure vs lot", fmt: "ratio3" },
  { key: "years_since_sale", label: "Years since last sale", fmt: "num" },
  { key: "lot_vs_zoning_min", label: "Lot vs zoning minimum", fmt: "ratio" },
  { key: "age_fit", label: "Age fit", fmt: "ratio" },
] as const;

export type Filters = {
  towns: string[];
  minScore: number;
  minCoverage: number;
  yearBuilt: [number, number];
  lotAcres: [number, number];
  minLandRatio: number;
  status: string[];
  hideDismissed: boolean;
  sewer: "any" | "sewer" | "septic";
  excludeFlood: boolean;
};

export const DEFAULT_FILTERS: Filters = {
  towns: [],
  minScore: 0,
  // A 0.97 score built from 45% of the component weight is a thinner claim
  // than a 0.95 built from all of it, so thin scores are hidden by default.
  minCoverage: 0.7,
  yearBuilt: [1850, new Date().getFullYear()],
  lotAcres: [0, 20],
  minLandRatio: 0,
  status: [],
  hideDismissed: true,
  sewer: "any",
  excludeFlood: false,
};

export const YEAR_MIN = 1850;
export const YEAR_MAX = new Date().getFullYear();
export const ACRE_MAX = 20;

/** Only the columns the table and map need; the detail panel fetches the row. */
export const LIST_COLUMNS = `
  loc_id || '|' || prop_id AS row_key, loc_id, prop_id, town, site_addr,
  score, score_coverage, year_built, res_area, lot_acres, land_val, total_val,
  val_land_ratio, ls_date, absentee, bldg_val_missing,
  flag_sewer, flag_wetland_m, flag_flood, lon, lat`;

export function whereClause(f: Filters, annotated: {
  dismissed: string[];
  byStatus: Record<string, string[]>;
}) {
  const w: string[] = [];
  if (f.towns.length)
    w.push(`town IN (${f.towns.map(sqlString).join(", ")})`);
  if (f.minScore > 0) w.push(`score >= ${f.minScore}`);
  if (f.minCoverage > 0) w.push(`score_coverage >= ${f.minCoverage}`);
  if (f.minLandRatio > 0)
    w.push(`val_land_ratio >= ${f.minLandRatio}`);
  if (f.yearBuilt[0] > YEAR_MIN)
    w.push(`year_built >= ${f.yearBuilt[0]}`);
  if (f.yearBuilt[1] < YEAR_MAX)
    w.push(`year_built <= ${f.yearBuilt[1]}`);
  if (f.lotAcres[0] > 0) w.push(`lot_acres >= ${f.lotAcres[0]}`);
  if (f.lotAcres[1] < ACRE_MAX) w.push(`lot_acres <= ${f.lotAcres[1]}`);
  // flag_sewer is null where DEP has not mapped the town at all, which is not
  // the same as septic, so both branches require a non-null value.
  if (f.sewer === "sewer") w.push("flag_sewer");
  if (f.sewer === "septic") w.push("flag_sewer IS FALSE");
  if (f.excludeFlood) w.push("flag_flood IS NULL");

  // Annotations live in localStorage, so status filtering is an id list
  // pushed into the query rather than a column.
  if (f.hideDismissed && annotated.dismissed.length)
    w.push(
      `loc_id || '|' || prop_id NOT IN (${annotated.dismissed
        .map(sqlString)
        .join(", ")})`,
    );
  if (f.status.length) {
    const ids = f.status.flatMap((s) => annotated.byStatus[s] ?? []);
    w.push(
      ids.length
        ? `loc_id || '|' || prop_id IN (${ids.map(sqlString).join(", ")})`
        : "false",
    );
  }
  return w.length ? `WHERE ${w.join(" AND ")}` : "";
}

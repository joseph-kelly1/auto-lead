/** Shared shapes and the filter -> SQL translation. */

import { sqlString } from "./duck";

export type Parcel = {
  row_key: string;
  loc_id: string;
  prop_id: string;
  town: string;
  site_addr: string | null;
  owner1: string | null;
  /** Alias of rebuild_opportunity_score; the tile layer joins on this name. */
  score: number;
  rebuild_opportunity_score: number;
  rebuild_feasibility_score: number;
  project_economics_score: number;
  acquisition_likelihood_score: number | null;
  structure_obsolescence_score: number | null;
  data_confidence_score: number;
  feasibility_notes: string | null;
  margin: number | null;
  profit: number | null;
  arv: number | null;
  acq_price: number | null;
  total_cost: number | null;
  buildable_sqft: number | null;
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
  estate: boolean;
  bldg_val_missing: boolean;
  flag_sewer: boolean | null;
  flag_wetland_m: number | null;
  flag_flood: string | null;
  flag_historic: string | null;
  flag_historic_area: string | null;
  n_records_on_parcel: number;
  lon: number;
  lat: number;
};

/** The five sub-scores, in the order the detail panel shows them. */
export const SUBSCORES = [
  {
    key: "rebuild_feasibility_score",
    label: "Rebuild feasibility",
    hint: "Can a replacement house be built. A hard blocker is a zero here, and a zero here is a zero overall.",
  },
  {
    key: "project_economics_score",
    label: "Project economics",
    hint: "Does the finished project pencil, in dollars, against local new-build sales.",
  },
  {
    key: "data_confidence_score",
    label: "Data confidence",
    hint: "How much of the above is actually known rather than estimated or missing.",
  },
  {
    key: "acquisition_likelihood_score",
    label: "Acquisition likelihood",
    hint: "Whether it is likely to trade. A supporting factor: it re-orders, it does not decide.",
  },
  {
    key: "structure_obsolescence_score",
    label: "Structure obsolescence",
    hint: "Whether keeping the existing house is economically weak. Supporting factor only.",
  },
] as const;

/** Structure-obsolescence components, which is all the old score used to be. */
export const COMPONENTS = [
  { key: "land_ratio", label: "Land share of value", fmt: "pct" },
  { key: "impr_psf", label: "Structure value per sq ft", fmt: "usd" },
  { key: "area_vs_nbhd", label: "Size vs neighbours", fmt: "ratio" },
  { key: "far", label: "Structure vs lot", fmt: "ratio3" },
  { key: "age_fit", label: "Age fit", fmt: "ratio" },
] as const;

/** Acquisition-likelihood components. */
export const ACQ_COMPONENTS = [
  { key: "years_since_sale", label: "Years since last sale", fmt: "num" },
] as const;

/** Feasibility gates, with what each one actually means on the ground. */
export const GATES: { key: string; label: string; note: string }[] = [
  {
    key: "unbuildable_envelope",
    label: "No viable envelope",
    note: "Lot area times the local floor-area ratio cannot reach a marketable house.",
  },
  {
    key: "historic_restriction",
    label: "Preservation restriction",
    note: "A recorded restriction runs with the land. Not a delay, a bar.",
  },
  {
    key: "historic_local_district",
    label: "Local historic district",
    note: "Demolition needs a certificate from the historic district commission.",
  },
  {
    key: "historic_district",
    label: "Historic area",
    note: "MHC-listed area, National Register designation. Usually the map a local delay bylaw points at.",
  },
  {
    key: "historic_inventory",
    label: "MHC-inventoried building",
    note: "Demolition delay bylaws hang off this list. Absence is not clearance: MHC coverage is very uneven.",
  },
  {
    key: "wetland_overlap",
    label: "Overlaps a wetland",
    note: "An NOI, a delineation and a Conservation Commission hearing before anyone draws a foundation.",
  },
  {
    key: "wetland_buffer",
    label: "In the 100 ft buffer zone",
    note: "Local Conservation Commission jurisdiction over the work.",
  },
  {
    key: "wetland_riverfront",
    label: "Near the 200 ft riverfront area",
    note: "Riverfront area review applies.",
  },
  {
    key: "flood_sfha",
    label: "FEMA flood zone",
    note: "Elevation certificates, flood insurance, and an engineer in a V zone.",
  },
  { key: "acec", label: "Area of Critical Environmental Concern", note: "Heightened review." },
  {
    key: "severely_undersized_lot",
    label: "Far below the zoning minimum",
    note: "The grandfathered envelope is probably what makes this lot usable, and a teardown can give it up.",
  },
  {
    key: "nonconforming_lot",
    label: "Below the zoning minimum",
    note: "Often buildable by right under G.L. c.40A s.6, but demolition can forfeit that status.",
  },
  {
    key: "septic_undersized",
    label: "Septic on a small lot",
    note: "Off municipal sewer and tight for a Title 5 system plus reserve area alongside a larger house.",
  },
  {
    key: "downzoned_existing",
    label: "Existing house exceeds the envelope",
    note: "A replacement would be smaller than what stands today, which inverts the economics.",
  },
  {
    key: "odd_shaped_lot",
    label: "Odd-shaped lot",
    note: "Sliver, flag lot or pipestem: the buildable area is not what the acreage suggests.",
  },
  {
    key: "narrow_lot",
    label: "Narrow lot",
    note: "Bounding-box width is short. A proxy for frontage, not frontage itself.",
  },
  {
    key: "shared_polygon",
    label: "Shared polygon",
    note: "Several single-family records on one polygon, so the lot area is not attributable to this one.",
  },
];

/** Pro forma line items, in the order they are incurred. */
export const PRO_FORMA = [
  { key: "acq_price", label: "Acquisition" },
  { key: "buy_cost", label: "Purchase costs" },
  { key: "demo_cost", label: "Demolition and abatement" },
  { key: "hard_cost", label: "Construction, site work, utilities" },
  { key: "soft_cost", label: "Design, permitting, contingency" },
  { key: "carry_cost", label: "Financing, taxes, insurance" },
  { key: "sale_cost", label: "Brokerage, closing, deeds excise" },
] as const;

export const SENSITIVITIES = [
  { key: "margin_if_acq", label: "Pay more for the site" },
  { key: "margin_if_cost", label: "Build over budget" },
  { key: "margin_if_value", label: "Sell under target" },
  { key: "margin_if_all", label: "All three at once" },
] as const;

export const PROVENANCE = [
  { key: "zoning", label: "Zoning and dimensional minimums" },
  { key: "resale_value", label: "Comparable new-build sales" },
  { key: "lot_area", label: "Lot area" },
  { key: "structure_value", label: "Structure value" },
  { key: "living_area", label: "Living area" },
  { key: "acquisition_price", label: "Acquisition price" },
  { key: "constraints", label: "Site constraints screened" },
  { key: "buildable_envelope", label: "Buildable envelope" },
] as const;

export const PROVENANCE_LABEL: Record<number, string> = {
  2: "authoritative",
  1: "estimated",
  0: "missing",
};

export type Filters = {
  towns: string[];
  minScore: number;
  minConfidence: number;
  minFeasibility: number;
  minMargin: number;
  requireProfitable: boolean;
  excludeBlocked: boolean;
  yearBuilt: [number, number];
  lotAcres: [number, number];
  minLandRatio: number;
  status: string[];
  hideDismissed: boolean;
  sewer: "any" | "sewer" | "septic";
  excludeFlood: boolean;
  excludeHistoric: boolean;
  excludeWetland: boolean;
};

export const DEFAULT_FILTERS: Filters = {
  towns: [],
  minScore: 0,
  // A score built from estimates is a thinner claim than one built from
  // authoritative inputs, so thin ones are hidden by default.
  minConfidence: 0.5,
  minFeasibility: 0,
  minMargin: 0,
  // The default view is the actual question: buildable AND profitable.
  requireProfitable: true,
  excludeBlocked: true,
  yearBuilt: [1850, new Date().getFullYear()],
  lotAcres: [0, 20],
  minLandRatio: 0,
  status: [],
  hideDismissed: true,
  sewer: "any",
  excludeFlood: false,
  excludeHistoric: false,
  excludeWetland: false,
};

export const YEAR_MIN = 1850;
export const YEAR_MAX = new Date().getFullYear();
export const ACRE_MAX = 20;

/** Only the columns the table and map need; the detail panel fetches the row. */
export const LIST_COLUMNS = `
  loc_id || '|' || prop_id AS row_key, loc_id, prop_id, town, site_addr,
  score, rebuild_opportunity_score, rebuild_feasibility_score,
  project_economics_score, acquisition_likelihood_score,
  structure_obsolescence_score, data_confidence_score, feasibility_notes,
  margin, profit, arv, acq_price, total_cost, buildable_sqft,
  year_built, res_area, lot_acres, land_val, total_val,
  val_land_ratio, ls_date, absentee, estate, bldg_val_missing,
  flag_sewer, flag_wetland_m, flag_flood, flag_historic, flag_historic_area,
  lon, lat`;

export function whereClause(f: Filters, annotated: {
  dismissed: string[];
  byStatus: Record<string, string[]>;
}) {
  const w: string[] = [];
  if (f.towns.length)
    w.push(`town IN (${f.towns.map(sqlString).join(", ")})`);
  if (f.minScore > 0) w.push(`score >= ${f.minScore}`);
  if (f.minConfidence > 0)
    w.push(`data_confidence_score >= ${f.minConfidence}`);
  if (f.minFeasibility > 0)
    w.push(`rebuild_feasibility_score >= ${f.minFeasibility}`);
  // A feasibility gate of zero is a hard blocker, not a low score.
  if (f.excludeBlocked) w.push("rebuild_feasibility_score > 0");
  if (f.requireProfitable) w.push("margin > 0");
  if (f.minMargin > 0) w.push(`margin >= ${f.minMargin}`);
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
  // Absence of an MHC record is not evidence a house is unlisted, so this
  // narrows the list without ever being able to clear a parcel.
  if (f.excludeHistoric)
    w.push("flag_historic IS NULL AND flag_historic_area IS NULL");
  if (f.excludeWetland)
    w.push("(flag_wetland_m IS NULL OR flag_wetland_m > 30.5)");

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

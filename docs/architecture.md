# Architecture

## Data contract

| File | Contents |
|---|---|
| `parcels.parquet` | One row per scored parcel, sorted by score. 146 columns |
| `parcels.pmtiles` | Parcel geometry in 4326 with `loc_id` and `score` only, enough to render and join back |
| `parcels_completeness.csv` | Per-town field coverage |

What is in the parquet, by group:

| Prefix | Contents |
|---|---|
| - | Raw assessor fields, plus `existing_sqft`, `lot_acres_used`, `lot_compactness`, `lot_width_ft`, `lot_depth_ft` |
| `flag_`, `fp_` | The six site screens, and building-footprint area, count and setback proxy |
| the six scores | `rebuild_opportunity_score` (= `score`), `rebuild_feasibility_score`, `project_economics_score`, `acquisition_likelihood_score`, `structure_obsolescence_score`, `data_confidence_score` |
| `gate_` | One boolean per feasibility gate, so the detail panel can say *which* constraint fired |
| `prov_` | Provenance per input: 2 authoritative, 1 estimated, 0 missing |
| pro forma | `arv`, `acq_price`, `est_acq_price`, `buy_cost`, `demo_cost`, `hard_cost`, `soft_cost`, `carry_cost`, `sale_cost`, `total_cost`, `profit`, `margin`, `return_on_cost` |
| `margin_if_` | Sensitivity to acquisition, cost, resale value, and all three at once |
| comps | `buildable_sqft`, `envelope_raw_sqft`, `observed_far`, `comp_count_local`, `comp_ppsf_local`, `comp_ppsf_town`, `comp_price_used`, `comp_sqft_used`, `acq_ratio_town`, `is_newbuild_comp`, `subdivision_lots` |
| `val_`, `pct_`, `nbr_` | Obsolescence component raw values, town percentiles, neighbourhood percentiles |
| - | `feasibility_notes`, a pipe-joined list of the gates that fired |

`score` is a duplicate of `rebuild_opportunity_score` under the name the tile
layer joins on. Both are immutable build outputs. Annotations live separately,
keyed on `loc_id|prop_id` in `localStorage` under `teardown-annotations-v1`, and
are never touched by a rebuild.

## How the data gets there

The browser fetches both artifacts directly, keeping bulk parcel data out of
server route handlers:

- **DuckDB-WASM** range-reads `parcels.parquet`. After the first load every
  filter change is a local query with no network round trip, so the whole
  result set is rendered rather than paged.
- **MapLibre** range-reads `parcels.pmtiles` through the `pmtiles` protocol.
  Browsers do not cache ranges against a single `.pmtiles` the way they cache
  individual tile files, so tiles refetch on reload. That is expected.

Tiles carry only `loc_id` and `score`; which parcels survive the filters is
known only to DuckDB and is pushed to the map as feature state. The join is on
`loc_id`. A polygon can carry multiple single-family records in the real dataset,
so the table's row key is `loc_id|prop_id`.

## Bundling notes

- `lib/duck.ts` imports `@duckdb/duckdb-wasm/dist/duckdb-browser.mjs` by exact
  subpath, at call time. The package root resolves to `duckdb-node.cjs` during
  Next's server pass over the client module, and that file contains a dynamic
  require webpack cannot follow.
- **No COOP/COEP headers**, deliberately. Those are only needed for DuckDB's
  cross-origin-isolated bundle, which uses SharedArrayBuffer. `selectBundle`
  picks mvp/eh instead, and setting COEP would break loading the worker and
  wasm from the CDN.
- `web/vercel.json` pins the framework preset. Authentication and market lookups
  use Next.js route handlers; parcel analytics run in the browser.

## Annotations

Status, notes, dismissal and the five vetting checks live in `localStorage`
under `teardown-annotations-v1`, keyed on
`loc_id|prop_id`. Export and import are in the rail; import merges rather than
replaces, last write wins per parcel by timestamp. Function filesystems are
ephemeral, so there is no server-side store to sync with, and for one person on
one machine a database would add little value. Provider credentials use a separate
encrypted server-side Blob record.

## Design tokens

Six colours, two typefaces, four sizes, all in `app/globals.css`.

| Token | Value | Role |
|---|---|---|
| `surface` | `#121614` | app background |
| `panel` | `#191e1b` | rail, table header, detail panel |
| `line` | `#2a322d` | borders and gridlines |
| `text` | `#dde4de` | primary |
| `muted` | `#7e8b83` | labels and secondary |
| `select` | `#e8b24c` | selection, and nothing else |

Score ramp: `#1c3a38 → #2c5c55 → #47857a → #78a986 → #b9c98e → #d8c48f`.

IBM Plex Sans for UI and body, the same face at 600 and tighter tracking for
display, IBM Plex Mono for identifiers only. Numbers use Plex Sans tabular
figures, which stays denser than mono in a table. Sizes: 11 / 13 / 15 / 22.

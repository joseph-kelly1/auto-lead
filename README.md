# Massachusetts teardown candidate screener

Finds residential parcels where the structure is economically worthless
relative to the land. Two pieces: an offline Python pipeline that turns MassGIS
Level 3 parcel data into two immutable artifacts, and a viewer that reads them.

Currently scoped to Newton, Wayland, Wellesley and Weston: 31,725 single-family
parcels.

## Pipeline

```
cd pipeline
uv sync
uv run build.py            # -> artifacts/parcels.parquet, parcels.pmtiles
uv run calibrate.py        # -> precision@K against historical teardowns
uv run build.py --selfcheck
```

Requires `tippecanoe` on PATH for the tile step (`brew install tippecanoe`);
everything else is a `uv sync` away. Source archives are cached in `data/raw/`
and reused, so re-runs are cheap. Delete a town's `.zip` to force a re-fetch.

### What it does

Downloads one `L3_SHP_M###_TOWN.zip` per configured town (~5 MB each) rather
than the 7.8 GB statewide aggregate. Joins the `TaxPar` polygon layer to the
`Assess` table on `LOC_ID`, filters to single family, computes eight signals,
percentile-ranks each *within municipality*, and writes a score.

All areas, distances and neighbourhoods are computed in **EPSG:26986** (Mass
State Plane Mainland, metres). Reprojection to 4326 happens once, at the tile
step.

### Configuration

Everything tunable lives in `pipeline/config.toml`: target towns, the eight
component weights, the age preference band, neighbourhood size, and the
per-town completeness floor. Change it and re-run; nothing is hardcoded.

Two optional inputs, both read from `pipeline/` if present:

- `zoning_minimums.csv` — `town,zoning,min_lot_acres,min_frontage_ft`.
  Without it `lot_vs_zoning_min` is null everywhere and its weight is
  redistributed. Note that `ZONING` in the assessor extract is the assessor's
  transcription, not the authoritative map, and Wellesley ships it empty; a
  spatial join against a town zoning layer in 26986 is more trustworthy.
- `demolition_permits.csv` — `town,loc_id,site_addr,permit_date`. A direct
  observation of the target event, preferred over inferred labels when
  available.

### Spatial pre-flags

`[flags] enabled = true` joins four MassGIS constraint layers against the
parcel polygons in 26986. They are screening markers, never conclusions, and
none of them feeds the score.

| Column | Layer | Meaning |
|---|---|---|
| `flag_sewer` | `DEP_Sewer_Service_Areas` (POTW only) | Parcel falls in a municipal sewer service area. **NULL** where DEP has not mapped the town at all, which is not the same as septic |
| `flag_wetland_m` | `wetlandsdep_poly` | Metres to the nearest DEP wetland; 0 means the parcel overlaps one. NULL beyond `wetland_search_m` |
| `flag_flood` | `nfhl` (current FEMA NFHL, not legacy Q3) | Flood zone code, restricted to `SFHA_TF = 'T'`. Zone X is minimal risk and would otherwise flag half the state |
| `flag_acec` | `acecs` | Name of the Area of Critical Environmental Concern, if any |

The layers total ~360 MB and are cached under `data/raw/layers/`. Set
`enabled = false` to skip them.

Sanity check against local knowledge, which is how you should read any of this:
Newton 99.8% and Wellesley 100% in a sewer service area; Wayland 0.8% and
Weston 0.6%, matching just the `WAYLAND SEWER` district and the two school
systems. Those two towns really are almost entirely septic. No ACEC comes
within 4.3 km of the four towns, so that column is legitimately empty here —
it will matter if you add Concord or Lincoln. The ACEC shapefile is dated 2009;
check for newer designations before relying on an absence.

`flag_wetland_m` at the default 250 m radius touches 94% of Weston and 89% of
Wayland, which is true but not actionable. The viewer therefore phrases the
distance by which rule it triggers: overlapping a resource area, inside the
100 ft Wetlands Protection Act buffer zone, near the 200 ft riverfront area, or
merely nearby.

### Data landmines this handles

These are the things that quietly produce wrong answers, all verified against
the real files rather than the spec:

| Trap | Handling |
|---|---|
| DBF numerics encode missing as `0`, not NULL | `0` becomes NULL for year built, areas, lot size, sale price |
| Newton reports `bldg_val = 0` on ~10% of single families that have living area and a year built | Treated as a suppressed structure value, not a worthless one; `land_ratio` and `impr_psf` go null and their weight redistributes |
| Use codes are 4 characters (`1010`, `1013`, `930V`), not 3 | Class is `left(code, 3)`, cross-checked against each town's `UC_LUT` |
| `LOT_SIZE` unit varies by row, carried in `LOT_UNITS` | Normalised to acres |
| `LS_DATE` is `C(8)` text; 226 rows are the sentinel `19000101` | Parsed, and anything before 1950 dropped |
| Newton's `CITY` holds village names while `OWN_CITY` says `NEWTON` | Absentee compares street address and state, never city |
| One polygon can carry hundreds of assessment rows | Condo and exempt filtering is applied at the polygon level, so a single condo record disqualifies the whole parcel |
| Wellesley ships `BLD_AREA` and `ZONING` entirely empty | Per-town completeness gate drops affected components; `far` falls back to living area |
| `LOC_ID` is `F_<x>_<y>`, US survey feet, while geometry is metres | Treated as an opaque key; coordinates never parsed out of it |

`artifacts/parcels_completeness.csv` reports per-town coverage for every
component, measured after these corrections, so it shows what scoring actually
saw.

### Calibration

`calibrate.py` re-scores the model on an older assessor vintage and asks
whether the parcels that were subsequently torn down ranked highly. Nothing
about the outcome leaks into the ranking.

Labels come from the statewide aggregate zip, which is an *archive* rather than
a snapshot: it holds every vintage a town has delivered (Newton has ten, CY13
through CY24). `httpzip.py` range-reads it so extracting four assessor tables
costs a few MB instead of 7.8 GB.

Two label sources, both computed, reported separately and unioned:

- `year_built` — `YEAR_BUILT` advanced by 15+ years. Near ground truth.
- `value_jump` — building value grew 3x faster than the town median over the
  same interval, which controls for revaluation inflation.

```
uv run calibrate.py --pairs 4 --labels year_built,value_jump
```

Longer horizons matter more than anything else: a candidate that has not been
torn down *yet* counts as a negative, so short windows understate precision
severely.

## Artifacts

| File | Contents |
|---|---|
| `parcels.parquet` | One row per scored parcel, sorted by score. Raw assessor fields, eight raw component values, town percentiles (`pct_`), neighbourhood percentiles (`nbr_`), composite `score` and `score_coverage` |
| `parcels.pmtiles` | Parcel geometry in 4326 with `loc_id` and `score` only, enough to render and join back |
| `parcels_completeness.csv` | Per-town field coverage |

Both are immutable build outputs. Annotations live separately, keyed on
`loc_id`, and are never touched by a rebuild.

`score_coverage` is the fraction of total component weight that was actually
available for that parcel. A missing component loses its weight rather than
scoring zero, so a town that did not populate a column is not pushed uniformly
to the bottom — but a parcel scoring 0.97 on 45% coverage is a thinner claim
than one scoring 0.95 on full coverage, and the viewer should let you filter on
it.

## Viewer

```
cd web
npm install
npm run artifacts      # serves ../artifacts on :8787 with ranges + CORS
npm run dev            # in a second shell
```

One screen: filter rail, map, table, and a detail panel that opens on
selection. Hovering a table row highlights the parcel; hovering a parcel
highlights the row and scrolls it into view. Escape closes the panel.

### How the data gets there

No parcel data passes through a Vercel Function — functions cap request and
response bodies at 4.5 MB and a single town's geometry exceeds that. The
browser fetches both artifacts directly:

- **DuckDB-WASM** range-reads `parcels.parquet`. After the first load every
  filter change is a local query with no network round trip, so the whole
  result set is rendered rather than paged.
- **MapLibre** range-reads `parcels.pmtiles` through the `pmtiles` protocol.
  Browsers do not cache ranges against a single `.pmtiles` the way they cache
  individual tile files, so tiles refetch on reload. That is expected.

Tiles carry only `loc_id` and `score`; which parcels survive the filters is
known only to DuckDB and is pushed to the map as feature state. The join is on
`loc_id`, and one Weston polygon carries two single-family records, so the
table's row key is `loc_id|prop_id`.

### Bundling notes

- `lib/duck.ts` imports `@duckdb/duckdb-wasm/dist/duckdb-browser.mjs` by exact
  subpath, at call time. The package root resolves to `duckdb-node.cjs` during
  Next's server pass over the client module, and that file contains a dynamic
  require webpack cannot follow.
- **No COOP/COEP headers**, deliberately. Those are only needed for DuckDB's
  cross-origin-isolated bundle, which uses SharedArrayBuffer. `selectBundle`
  picks mvp/eh instead, and setting COEP would break loading the worker and
  wasm from the CDN.
- `web/vercel.json` pins the framework preset for reproducible Git-based
  Vercel builds. No functions, rewrites or custom headers are needed.

### Deploying

Deployment uses Vercel's Git integration; there is no CLI deployment step.

1. Commit and push the repository to GitHub.
2. In Vercel, choose **Add New → Project** and import this repository.
3. Set **Root Directory** to `web`.
4. Leave Framework Preset as **Next.js** and leave the build, install and
   output settings at their detected defaults.
5. Deploy. Pushes to `main` become production deployments; other branches and
   pull requests get preview deployments.

The required `web/public/parcels.parquet` and `web/public/parcels.pmtiles`
files are checked into Git so a clean Vercel clone has everything it needs.
After rebuilding the pipeline data, run `cd web && npm run stage`, then commit
the refreshed copies.

`npm run build` runs `npm run stage` first, which copies the two artifacts from
`../artifacts` into `public/`. On Vercel, it validates and uses the checked-in
copies instead.

The four-town artifacts are 13 MB, comfortably inside the 100 MB Vercel
source-upload cap, so they ship with the deployment and are served same origin.
**Vercel's static CDN does honour range requests** — verified, HTTP 206 with a
correct `content-range` — so DuckDB-WASM and pmtiles both work from `public/`.

Move to R2 when the dataset grows past roughly 50 MB, or when re-uploading it on
every CSS change starts to annoy you:

1. Upload both artifacts to an R2 bucket. No egress fees, documented range
   support, and no re-upload on unrelated changes.
2. CORS rule allowing `GET, HEAD`, allowing the `range` and `if-match` request
   headers, exposing `etag`.
3. Set `NEXT_PUBLIC_ARTIFACT_BASE` to the bucket URL in the Vercel dashboard.
   It is inlined at build time, so redeploy after changing it.
4. Drop the `stage` step from `build` and delete `public/parcels.*`.

`artifacts/` and `data/` are gitignored; only the two deployment copies under
`web/public/` are versioned. To test the R2 path locally, `npm run artifacts` serves
`../artifacts` on :8787 with ranges and CORS — point
`NEXT_PUBLIC_ARTIFACT_BASE` at it.

### Annotations

Status, notes, dismissal and the five vetting checks are the only writes, and
they live in `localStorage` under `teardown-annotations-v1`, keyed on
`loc_id|prop_id`. Export and import are in the rail; import merges rather than
replaces, last write wins per parcel by timestamp. Function filesystems are
ephemeral, so there is no server-side store to sync with, and for one person on
one machine a database would be infrastructure with no user.

### Design tokens

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

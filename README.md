# Massachusetts teardown-to-rebuild opportunity screener

Finds single-family parcels that are good candidates to **buy, demolish, and
rebuild for resale**. That is a different question from "which houses look
demolishable", and the model is built so that the difference shows: a parcel
cannot rank highly because its existing structure is worthless. It has to be
buildable, and the completed house has to cover what the project costs.

Two pieces: an offline Python pipeline that turns MassGIS Level 3 parcel data
into two immutable artifacts, and a viewer that reads them.

Currently scoped to Newton, Wayland, Wellesley and Weston: 31,725 single-family
parcels.

## The question it answers

> Which properties are the best complete teardown-and-rebuild opportunities,
> after considering buildability, acquisition cost, total project cost,
> completed value, profit potential, seller likelihood and data confidence?

Six scores per parcel, not one opaque number:

| Column | Meaning |
|---|---|
| `rebuild_feasibility_score` | Can a replacement house be built. Multiplicative gates; a hard blocker is a literal 0 |
| `project_economics_score` | Does the finished project pencil, in dollars, against local new-build sales |
| `acquisition_likelihood_score` | Will it trade, and when |
| `structure_obsolescence_score` | Is keeping the existing house economically weak |
| `data_confidence_score` | How much of the above is known rather than estimated or missing |
| `rebuild_opportunity_score` | The ranking. Also written as `score`, which is what the tile layer joins on |

```
rebuild_opportunity_score =
      feasibility ^ 1.0
    * economics   ^ 1.0
    * confidence  ^ 0.5
    * (1 + 0.35 * support) / 1.35        support = 60% acquisition + 40% obsolescence
```

The first three multiply, which is the whole design. A feasibility gate of 0
forces the final score to 0, and no amount of seller motivation or structural
obsolescence can lift a parcel that cannot be rebuilt or cannot pencil.
Obsolescence and acquisition likelihood span a bounded `[1, 1.35]` re-order
among parcels that already clear the core.

Every weight, cost and threshold lives in `pipeline/config.toml`. **None of them
were learned from outcome data** — see [Validation](#validation) for what the
labels do and do not support, including the one assumption that *is* checked
against outcomes.

## Pipeline

```
cd pipeline
uv sync
uv run build.py            # -> artifacts/parcels.parquet, parcels.pmtiles
uv run build.py --selfcheck
uv run model.py            # model-only self-check, no data needed
uv run calibrate.py        # -> precision@K against historical rebuilds
```

Requires `tippecanoe` on PATH for the tile step (`brew install tippecanoe`);
everything else is a `uv sync` away. Source archives are cached in `data/raw/`
and reused, so re-runs are cheap. Delete a town's `.zip` to force a re-fetch.

### What it does

Downloads one `L3_SHP_M###_TOWN.zip` per configured town (~5 MB each) rather
than the 7.8 GB statewide aggregate. Joins the `TaxPar` polygon layer to the
`Assess` table on `LOC_ID`, filters to single family, screens each parcel
against six MassGIS layers, builds a comparable set from recorded
new-construction sales, runs a full project pro forma, and writes six scores.

All areas, distances and neighbourhoods are computed in **EPSG:26986** (Mass
State Plane Mainland, metres). Reprojection to 4326 happens once, at the tile
step.

`model.py` holds the scoring itself and touches no file or database: every
function takes numpy arrays, so `build.py` scores the current release,
`calibrate.py` re-scores a 2016 vintage, and `uv run model.py` scores six
hand-written rows as a self-check.

### Rebuild feasibility

Multiplicative gates, each a configured multiplier in `[feasibility]`, so
several constraints compound. `0.0` is a hard blocker and forces the final
score to zero.

Unknown constraints normally do not apply a multiplier. **Lot size is the
exception:** a candidate must have both a measured parcel area and a matched
municipal minimum. Missing either one fires `lot_size_unverified` and blocks the
candidate rather than treating unknown as conforming.

Gates, and how many parcels each one fired on in the shipped four-town build:

| Gate | × | Parcels | Source |
|---|---|---|---|
| `lot_size_unverified` | 0.00 | 190 | parcel area or applicable district minimum is unknown |
| `nonconforming_lot` | 0.00 | 11,666 | lot is 60–100% of the applicable zoning minimum |
| `severely_undersized_lot` | 0.00 | 5,358 | lot is below 60% of the applicable zoning minimum |
| `unbuildable_envelope` | 0.00 | 774 | lot × local FAR cannot reach a marketable house |
| `historic_restriction` | 0.00 | 15 | recorded preservation restriction (MHC `PR`) |
| `historic_local_district` | 0.05 | 232 | MHC area designated `LHD` |
| `wetland_overlap` | 0.10 | 1,897 | parcel intersects a MassDEP wetland |
| `flood_sfha` | 0.15 | 1,811 | mapped FEMA Special Flood Hazard Area |
| `historic_district` | 0.20 | 3,384 | MHC historic area, National Register designation |
| `septic_undersized` | 0.25 | 2,588 | off sewer and below `septic_min_acres` |
| `historic_inventory` | 0.30 | 1,798 | MHC-inventoried building on the parcel |
| `acec` | 0.30 | 0 | Area of Critical Environmental Concern |
| `narrow_lot` | 0.30 | 5,149 | bounding-box width below district frontage minimum or fallback |
| `odd_shaped_lot` | 0.35 | 210 | compactness `4πA/P²` below `min_compactness` |
| `shared_polygon` | 0.50 | 2 | several single-family records on one polygon |
| `downzoned_existing` | 0.55 | 3,294 | existing living area exceeds the local envelope |
| `wetland_buffer` | 0.55 | 1,765 | inside the 100 ft Wetlands Protection Act buffer |
| `wetland_riverfront` | 0.85 | 1,921 | within the 200 ft riverfront area |

Municipal bylaw tables match 31,535 of 31,725 parcels. Newton and Wellesley
districts come from official GIS centroid joins; Wayland and Weston use the
district carried in the assessor extract and are marked estimated provenance.
The shipped standards use the stricter current/new-lot requirement where a
bylaw varies by lot-creation date, so this screen prefers false negatives over
clearing a legally nonconforming teardown.

The **existing house is never a floor on the envelope**. If the observed local
floor-area ratio implies something smaller than what already stands, that is the
`downzoned_existing` case and the pro forma feels it.

### Project economics

A dollar pro forma per parcel, every line item written to the parquet.

**Completed value** comes from recorded new-construction sales in the assessor's
own extract: a parcel whose `YEAR_BUILT` is recent and whose `LS_DATE` falls
within a few years of that year is a new-home sale. Public record, never
scraped, and no comparable is ever synthesised — a town without enough of them
gets a null resale value and a report line saying so.

Comparables are drawn from a nearest-neighbour search over the comp set itself,
not over all parcels: new builds are ~1.5% of these towns, so a 200-parcel
neighbourhood holds two of them. Value is then a paired-sales adjustment:

```
ARV = comparable price + marginal_sqft_share × $/sqft × (our sqft − comparable sqft)
```

bounded so the implied `$/sqft` stays inside `[0.75, 1.25] ×` the comparables'
own. Straight `$/sqft × floor area` is wrong at the small end — a new 2,000 sqft
house in Newton does not sell for 2,000 × $640, because most of the comparable's
price is land our lot also has — and unbounded the adjustment let a 2,800 sqft
envelope imply $1,283/sqft in a town whose new builds trade at $877.

Per town, from the shipped build:

| Town | Comps | Own trend | Leave-one-out error | $/sqft | Observed FAR | Assess→sale |
|---|---|---|---|---|---|---|
| Newton | 380 | 6.4% | 21.0% | $877 | 0.39 | 1.065 |
| Wayland | 36 | 8.1% | 14.8% | $570 | 0.14 | 1.098 |
| Wellesley | 250 | 7.2% | 20.4% | $903 | 0.29 | 1.067 |
| Weston | 101 | 9.3% | 33.0% | $836 | 0.09 | 0.999 |

Accuracy is reported as **leave-one-out median absolute percentage error** —
each comparable's own recorded price predicted from the others — because that is
out-of-sample error on real sales rather than the in-sample R² of a fit nobody
can check. It runs 15–33%. Treat the value as a screen, not an appraisal.

The price trend is fitted **pooled across towns** by default. Weston's own slope
fits at 9.3%/yr on 101 comps, which doubles an eight-year-old sale and produced
$7.7M resale values; and no single comparable may be time-adjusted by more than
`max_trend_adjustment`. Filtering nominal-consideration deeds matters more than
it sounds: `LS_PRICE` carries thousands of $1 and $100 family transfers, and
left in they invert the sign of the fitted trend.

**Acquisition price** is a live list price where `listings.csv` supplies one,
otherwise assessed value × the town's *measured* sale-to-assessment ratio (these
four towns land at 0.999–1.098) × an off-market premium. Where the assessor
suppresses the structure value the total assessment is depressed with it and the
multiple reads as a bargain — 1,637 Newton parcels, one of which priced out at a
$356k acquisition. Those fall back to land value grossed up by the town's
measured sale-to-land-value ratio.

**Costs** are all in `[costs]`: demolition per square foot of gross area,
pre-1978 abatement, construction per square foot of the envelope, site work,
septic where the parcel is off sewer, utilities, design and permitting,
contingency, carry (interest, taxes, insurance), brokerage, closing and the
Massachusetts deeds excise. Sensitivity columns re-run the whole pro forma with
a higher acquisition, a cost overrun, a lower sale price, and all three at once.

Margin maps to `project_economics_score` linearly between `min_margin` and
`target_margin`. Below `min_margin` it drops onto a decaying tail capped at
`unprofitable_ceiling` — an unprofitable project cannot rank, but forcing 90% of
a town to exactly `0.0` would leave 30,000 tied rows and no ordering at all.

### Data confidence

Every input the score depends on is tracked as **authoritative / estimated /
missing** and reported per parcel in `prov_*` columns and in the detail panel.
Confidence is the weighted provenance share, and it *multiplies* into the final
score, so unknowns cost rank rather than being silently treated as favourable.

Shipped build, share of parcels:

| Input | Authoritative | Estimated | Missing |
|---|---|---|---|
| `zoning` | 76.0% | 23.8% | 0.1% |
| `resale_value` | 91.1% | 8.9% | 0.0% |
| `lot_area` | 99.7% | 0.3% | 0.0% |
| `structure_value` | 94.6% | 0.0% | 5.4% |
| `living_area` | 76.9% | 23.1% | 0.0% |
| `acquisition_price` | 0.0% | 100.0% | 0.0% |
| `constraints` | 100.0% | 0.0% | 0.0% |
| `buildable_envelope` | 66.1% | 33.9% | 0.0% |

Acquisition price remains the largest systematic data gap: no live listings
export is loaded, so every acquisition estimate comes from assessments. Zoning
is authoritative where an official municipal GIS assignment and bylaw minimum
both exist; assessor-carried districts remain estimated even when their
dimensional standard comes from an official bylaw.

### Configuration

Everything tunable lives in `pipeline/config.toml`, grouped by concept:
`[score]`, `[feasibility]`, `[buildable]`, `[comps]`, `[acquisition]`,
`[costs]`, `[economics]`, `[obsolescence]`, `[acquisition_likelihood]`,
`[confidence]`. Change it and re-run; nothing is hardcoded, and every section
carries the reasoning for its numbers.

Local inputs are read from `pipeline/`, and the build prints which are present,
missing, or unmatched. Zoning standards and official Newton/Wellesley parcel
assignments now ship with the repository; listings and permit exports remain
optional.

| File | Used by | Effect when present |
|---|---|---|
| `zoning_assignments.csv` | build | `town,loc_id,zoning,authority,source_url,as_of`. Official parcel-centroid district assignments for Newton and Wellesley. Refresh from municipal ArcGIS layers with `uv run fetch_zoning_assignments.py` |
| `zoning_minimums.csv` | build | `town,zoning,min_lot_acres,min_frontage_ft,max_far,max_lot_coverage,authority,source_url,as_of`. Official lot-area/frontage standards for the four towns. Missing or undersized lots are hard blockers; matched FAR/coverage also makes the envelope authoritative |
| `listings.csv` | build | `town,loc_id,site_addr,status,list_price,original_list_price,list_date,days_on_market`. A real list price replaces the assessment multiple and is marked authoritative; enables the four listing components of acquisition likelihood, which are null without it |
| `permits.csv` | calibrate | `town,loc_id,site_addr,permit_date,permit_type` where `permit_type` is `demolition` or `new_dwelling`. The only direct observation of the target event. A demolition joined to a subsequent new dwelling is a success; a demolition with nothing built back is an explicit negative |

Nothing in the repository fabricates zoning, cost, permit or comparable-sale
data. Where a source is absent, the affected component is null and confidence
falls.

### Site screening

`[flags] enabled = true` joins six MassGIS layers against the parcel polygons in
26986. Unlike the earlier version, these now **feed
`rebuild_feasibility_score` and the buildable envelope**, so a NULL matters: it
means nobody looked, not that the site is clear.

| Column | Layer | Meaning |
|---|---|---|
| `flag_sewer` | `DEP_Sewer_Service_Areas` (POTW only) | Parcel falls in a municipal sewer service area. **NULL** where DEP has not mapped the town at all, which is not the same as septic |
| `flag_wetland_m` | `wetlandsdep_poly` | Metres to the nearest DEP wetland; 0 means the parcel overlaps one. NULL beyond `wetland_search_m` |
| `flag_flood` | `nfhl` (current FEMA NFHL, not legacy Q3) | Flood zone code, restricted to `SFHA_TF = 'T'`. Zone X is minimal risk and would otherwise flag half the state |
| `flag_acec` | `acecs` | Name of the Area of Critical Environmental Concern, if any |
| `flag_historic`, `flag_historic_area`, `flag_historic_designations` | `mhcinv` | MHC cultural resource inventory. Points are inventoried buildings, polygons are historic areas; `LHD` means a local historic district and `PR` a recorded preservation restriction |
| `fp_area_sqft`, `fp_count`, `fp_min_setback_ft` | `buildingfp_poly` | Building footprints, attributed by centroid containment, with the minimum distance to the parcel boundary as a setback proxy |

The layers total ~400 MB and are cached under `data/raw/layers/`. Set
`enabled = false` to skip them; the build still works and every parcel simply
reports lower confidence.

**Coverage caveats that matter more than the layers themselves.** MHC inventory
is very uneven — Newton has 155 inventoried points against 16,944 single
families, Wellesley 1,193 against 7,319 — so an absent MHC record is *not*
evidence a house is unlisted, and the layer can only ever add a constraint,
never clear one. Building footprints reached only 5,673 of 31,725 parcels;
statewide coverage is partial, and the columns go null rather than zero outside
it. The ACEC shapefile is dated 2009; check for newer designations before
relying on an absence.

Sanity check against local knowledge, which is how you should read any of this:
Newton 99.8% and Wellesley 100% in a sewer service area; Wayland 0.8% and
Weston 0.6%, matching just the `WAYLAND SEWER` district and the two school
systems. Those two towns really are almost entirely septic. No ACEC comes
within 4.3 km of the four towns, so that column is legitimately empty here —
it will matter if you add Concord or Lincoln.

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
| Newton reports `bldg_val = 0` on ~10% of single families that have living area and a year built | Treated as a suppressed structure value, not a worthless one. `land_ratio` and `impr_psf` go null, structure value counts as missing for confidence, **and** the acquisition estimate switches from total assessment to land value — the depressed total otherwise reads as a $356k Newton bargain. These parcels are also 5.7× more likely to be rebuilt than base rate, because `bldg_val = 0` often means the structure is *already* gone |
| `LS_PRICE` carries thousands of $1 and $100 family transfers | Filtered by `comps.min_price` everywhere a sale is used. Left in they invert the sign of the fitted price trend |
| `BLD_AREA` is gross area and `RES_AREA` is living area, differing by ~60% | Demolition cost uses gross; the envelope comparison uses living, because the envelope is inferred from the living area of new builds. Mixing them flagged most of Newton as downzoned |
| Use codes are 4 characters (`1010`, `1013`, `930V`), not 3 | Class is `left(code, 3)`, cross-checked against each town's `UC_LUT` |
| `LOT_SIZE` unit varies by row, carried in `LOT_UNITS` | Normalised to acres |
| `LS_DATE` is `C(8)` text; 226 rows are the sentinel `19000101` | Parsed, and anything before 1950 dropped |
| Newton's `CITY` holds village names while `OWN_CITY` says `NEWTON` | Absentee compares street address and state, never city |
| One polygon can carry hundreds of assessment rows | Condo and exempt filtering is applied at the polygon level, so a single condo record disqualifies the whole parcel |
| Wellesley ships `BLD_AREA` and assessor `ZONING` entirely empty | `far` falls back to living area; zoning is assigned from Wellesley's official GIS district polygons |
| `LOC_ID` is `F_<x>_<y>`, US survey feet, while geometry is metres | Treated as an opaque key; coordinates never parsed out of it |

`artifacts/parcels_completeness.csv` reports per-town coverage for every
component, measured after these corrections, so it shows what scoring actually
saw.

### Validation

```
uv run calibrate.py --pairs 4                    # default target: rebuild_sold
uv run calibrate.py --pairs 4 --target viable --fit
```

`calibrate.py` re-scores the model on an older assessor vintage and asks whether
the parcels that were subsequently **rebuilt** ranked highly. Nothing about the
outcome enters the ranking. Labels come from the statewide aggregate zip, which
is an *archive* rather than a snapshot: it holds every vintage a town has
delivered. `httpzip.py` range-reads it so four assessor tables cost a few MB
instead of 7.8 GB.

#### What counts as success

"Was it demolished" is **not** the target. A demolition with no replacement house
is a failure and is counted as a negative.

| Label | Definition | Counted as success |
|---|---|---|
| `rebuild` | `YEAR_BUILT` advanced ≥15 years **and** the structure changed (living area moved ≥10% or building value at least doubled) | yes |
| `rebuild_sold` | `rebuild`, and the finished house then sold as new construction. **Default target** | yes |
| `viable` | `rebuild_sold`, and the recorded sale price cleared the modelled total project cost | yes |
| `permits` | demolition permit joined to a subsequent new-dwelling permit | yes, when `permits.csv` exists |
| `demo_only` | demolition permit with nothing built back | **explicit negative**, reported separately |
| `value_jump` | building value grew 3× faster than the town median | **no** — mostly large renovations, reported for reference only |

The structure-changed half of `rebuild` matters: an assessor correcting a wrong
`YEAR_BUILT` is not a rebuild. And the resale test needs two conditions, because
`LS_PRICE` holds only the *last* sale: it must be dated at or after the build
year **and** priced like a finished house (≥85% of the new total assessment).
Without the second test, builders' land purchases pass as finished-home sales —
one example property's last recorded sale is an $880k lot purchase in the same year its
new house was finished.

Label volume at the deepest window (4.5 fiscal years, four towns):
`rebuild` 600, `rebuild_sold` 311, `viable` 158.

#### The one assumption checked against outcomes

For every parcel that was rebuilt and sold, the resale price and the built floor
area are both observed. Solving the pro forma for the construction rate that
would have made each of those projects break even gives a distribution:

| Town | n | Break-even $/sqft (p25 / median / p75) | At a 20% margin |
|---|---|---|---|
| Newton | 144 | $315 / $376 / $441 | $241 |
| Wayland | 9 | $152 / $173 / $212 | $105 |
| Wellesley | 135 | −$43 / $312 / $440 | $180 |
| Weston | 23 | $287 / $391 / $467 | $244 |
| **All** | **311** | **$252 / $351 / $439** | **$222** |

`costs.construction_per_sqft` is therefore set to **$350**, that median, rather
than to a plausible-sounding guess. At the original $425 the model called 96% of
real completed rebuilds unprofitable, which collapsed
`project_economics_score` and with it the ranking. Every run reprints this table
and warns if the configured rate sits above the median. This is calibration of an
assumption against outcomes — it is not a fitted weight.

#### Ranking performance, and an honest negative result

Time-ordered: the model is scored on a prior vintage and outcomes read from the
current release, so nothing leaks. `--fit` splits further, training on the older
vintages and validating on the most recent prior one.

Deepest window, `rebuild_sold`, 302 successes in 27,439 parcels (1.10% base):

| Ranking by | AUC | P@50 | P@100 | P@250 | recall@250 |
|---|---|---|---|---|---|
| `rebuild_opportunity_score` (this model) | 0.674 | 2.0% | 1.0% | 0.8% | 0.7% |
| `structure_obsolescence_score` (the old model) | **0.773** | **12.0%** | **12.0%** | **7.6%** | 6.3% |
| `project_economics_score` alone | 0.655 | 2.0% | 1.0% | 0.8% | 0.7% |
| `rebuild_feasibility_score` alone | 0.582 | 0.0% | 0.0% | 1.6% | 1.3% |
| `acquisition_likelihood_score` alone | 0.453 | 0.0% | 1.0% | 1.2% | 1.0% |

**The obsolescence-only ranking predicts observed rebuilds substantially better
than the economics-driven one.** That result held after fixing four real
modelling bugs, so it is a property, not a defect, and it is worth stating
plainly rather than burying. Three things are going on:

1. **Part of the old score's edge is leakage.** Parcels whose structure value is
   suppressed are rebuilt at 6.04% against a 1.06% base rate, because
   `bldg_val = 0` frequently means the house is already gone. 32 of the old
   model's top 50 are such parcels. Excluding them the old model still wins, but
   by less.
2. **The label measures occurrence, not quality.** "Which cheap obsolete houses
   did builders buy" is largely a demand signal, and obsolescence measures it
   directly. A genuinely excellent site that nobody has got to yet counts as a
   negative, and the model is explicitly built to surface those.
3. **The economics is a noisy multiplier.** Resale value carries 15–33%
   out-of-sample error and acquisition price is modelled, not observed. A margin
   built from two noisy estimates has a tail dominated by estimation error, and
   `precision@50` lives entirely in that tail.

Every run prints a sensitivity sweep of the two levers that decide which side
wins, so the trade-off is measured rather than argued. Deepest window:

```
economics_exponent    0  AUC 0.619 P@100 4.0%   0.25  AUC 0.672 P@100 1.0%   1* AUC 0.674 P@100 1.0%   1.5  AUC 0.675 P@100 1.0%
support_strength      0  AUC 0.672 P@100 1.0%   0.35* AUC 0.674 P@100 1.0%   1  AUC 0.676 P@100 1.0%   4  AUC 0.680 P@100 1.0%
obsolescence only        AUC 0.773 P@100 12.0%                                            (* = configured)
```

`--fit` grid-searches 480 combinations of the combination weights on the training
split and reports both splits. It **refuses to fit below 150 training successes**
rather than dressing up a fit on forty labels as optimisation. On the current
data it does run, and it lands here:

```
                        AUC     P@50    P@100   P@250
train      config      0.679    0.0%    1.0%    1.6%
train      fitted      0.714   10.0%    7.0%    4.4%
validation config      0.619    0.0%    0.0%    0.4%
validation fitted      0.747    0.0%    0.0%    0.8%
fitted params: economics_exponent 0.0, confidence_exponent 0.0, support_strength 0.35
```

The search wants to switch the pro forma off entirely, which is the obsolescence
model wearing a different name. It improves training precision and validation
AUC, and **does not improve validation precision@50 or @100 at all** — so it is
reported as a candidate and not adopted.

The defaults have therefore been left as configured expert settings, not retuned
to win this metric, for the reason in point 2: the brief is economically viable
projects, not demolition occurrence. If you would rather rank on the empirically
stronger occurrence signal, lower `score.economics_exponent` toward 0 and raise
`score.support_strength` — the numbers above tell you what that costs and buys.

Longer horizons matter more than anything else: a candidate that has not been
rebuilt *yet* counts as a negative, so short windows understate precision
severely. Results are reported separately by town on every run.

## Artifacts

| File | Contents |
|---|---|
| `parcels.parquet` | One row per scored parcel, sorted by score. 146 columns |
| `parcels.pmtiles` | Parcel geometry in 4326 with `loc_id` and `score` only, enough to render and join back |
| `parcels_completeness.csv` | Per-town field coverage |

What is in the parquet, by group:

| Prefix | Contents |
|---|---|
| — | Raw assessor fields, plus `existing_sqft`, `lot_acres_used`, `lot_compactness`, `lot_width_ft`, `lot_depth_ft` |
| `flag_`, `fp_` | The six site screens, and building-footprint area, count and setback proxy |
| the six scores | `rebuild_opportunity_score` (= `score`), `rebuild_feasibility_score`, `project_economics_score`, `acquisition_likelihood_score`, `structure_obsolescence_score`, `data_confidence_score` |
| `gate_` | One boolean per feasibility gate, so the detail panel can say *which* constraint fired |
| `prov_` | Provenance per input: 2 authoritative, 1 estimated, 0 missing |
| pro forma | `arv`, `acq_price`, `est_acq_price`, `buy_cost`, `demo_cost`, `hard_cost`, `soft_cost`, `carry_cost`, `sale_cost`, `total_cost`, `profit`, `margin`, `return_on_cost` |
| `margin_if_` | Sensitivity to acquisition, cost, resale value, and all three at once |
| comps | `buildable_sqft`, `envelope_raw_sqft`, `observed_far`, `comp_count_local`, `comp_ppsf_local`, `comp_ppsf_town`, `comp_price_used`, `comp_sqft_used`, `acq_ratio_town`, `is_newbuild_comp`, `subdivision_lots` |
| `val_`, `pct_`, `nbr_` | Obsolescence component raw values, town percentiles, neighbourhood percentiles |
| — | `feasibility_notes`, a pipe-joined list of the gates that fired |

`score` is a duplicate of `rebuild_opportunity_score` under the name the tile
layer joins on. Both are immutable build outputs. Annotations live separately,
keyed on `loc_id|prop_id` in `localStorage` under `teardown-annotations-v1`, and
are never touched by a rebuild.

`data_confidence_score` replaces the old `score_coverage`. The old column had a
quiet bug worth knowing about if you have artifacts from before this change: the
inert `lot_vs_zoning_min` weight was included in its denominator but never
available, so coverage topped out at 0.80 for every parcel in the dataset while
the viewer's default filter compared it against 0.7.

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

The table leads with margin, profit, completed value and acquisition price,
because those are the columns the decision actually turns on. The detail panel
shows, in order: the five sub-scores with what each one means, the full pro forma
line by line with its sensitivities, **which** feasibility gates fired and why
each one matters on the ground, the provenance of every input, the assessment,
and only then the obsolescence signals that used to be the whole score.

Two filters are on by default — *hide hard feasibility blockers* and *only
projects that pencil* — because the default view should answer the question the
tool exists for. Turn them off to see the whole universe.

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
4. Open the project's **Storage** tab, create a **Blob** store with access set
   to **Private**, and connect it to this project for the Production and Preview
   environments you intend to use.
5. Add a sensitive `APP_ACCESS_KEY` environment variable containing at least
   24 random characters. Add it to Production and Preview if both should be
   protected.
6. Leave Framework Preset as **Next.js** and leave the build, install and
   output settings at their detected defaults.
7. Deploy. Pushes to `main` become production deployments; other branches and
   pull requests get preview deployments.

The app uses that one shared key as its login. A successful login creates a
signed, HttpOnly 30-day session cookie. The key is server-only: do not hardcode
it or prefix it with `NEXT_PUBLIC_`, because either would expose it in Git or
the browser bundle.

After signing in, **API credentials** at the bottom of the filter rail accepts
a Zillow/Bridge server token plus an MLS PIN/Bridge server token and dataset
code. The app encrypts these with `APP_ACCESS_KEY` before writing one shared
record to the private Blob store. They are available to both users on every
browser and are never returned to client-side JavaScript. Rotating
`APP_ACCESS_KEY` also rotates the encryption key, so re-enter the provider
credentials afterward.

When configured, opening a property queries the official Bridge Zestimate and
RESO Web API endpoints. The detail panel shows Zestimate, estimate range, MLS
status, list/close price and listing ID. Results are cached locally for 24
hours. With no provider credentials, the market section stays hidden and no
provider request is made. **Zillow is never scraped**; only the authorised
Bridge API is called.

This lookup is per-parcel, on demand, and deliberately **does not feed the
ranking**. The score is computed offline over 31,725 parcels and the app has to
work with no provider credentials at all, so a live per-parcel call cannot be a
scoring input. To let a real list price into the pro forma — where it replaces
the assessment multiple and is marked authoritative — export listings to
`pipeline/listings.csv` and rebuild. The detail panel says as much where the
market section renders.

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

## What the model still needs

Ranked by how much each one would improve the answer, not by how easy it is to
get. The first three are where the model is currently guessing hardest, and all
three are public records.

**1. Full buildable envelopes and parcel-specific rebuild rights.** Minimum lot
area is now enforced as a hard eligibility rule, and official district geometry
is joined for Newton and Wellesley. The model also uses bylaw FAR or coverage
limits where the source table supplies them. Remaining work: authoritative
Wayland and Weston district geometry, lot-creation dates, setbacks, height,
variances, and determinations of grandfathered status. Until those facts are
verified, an unmatched or undersized lot is blocked rather than treated as a
plausible teardown.

**2. Building permits — demolition joined to new dwelling.** The only direct
observation of the target event, and the label the validation most wants. Today
success is inferred from `YEAR_BUILT` moving plus a structure change, which works
but is coarse and cannot see a demolition that never got rebuilt, cannot date the
project, and cannot distinguish a rebuild from a gut renovation with certainty.
Sources: each town's building department, often via an OpenGov / Accela / Viewpoint
portal; the state's construction permit reporting. Load into
`pipeline/permits.csv` with `permit_type`.

**3. Listings and closed sales, with history.** `acquisition_price` is 100%
estimated. An assessment multiple is a decent central estimate and a bad guide to
any individual deal, and days-on-market, price cuts, withdrawn and expired
listings are the whole timing half of acquisition likelihood — all null today.
Closed new-construction sales would also replace the assessor's single
`LS_PRICE`/`LS_DATE` per parcel, which is the reason the comparable set is only
~100 sales per town and the resale model carries 15–33% error. Source: your MLS
via an authorised RESO Web API feed. Load into `pipeline/listings.csv`.

**4. A builder's actual cost schedule.** `costs.construction_per_sqft` is
currently set to the median break-even of 311 real completed projects, which is a
sanity check rather than a quote. Real numbers per square foot by product type,
plus site work, demolition, abatement and septic, would move the pro forma from
plausible to usable. No dataset needed — one conversation.

**5. Wetland delineations and conservation decisions.** The DEP wetlands layer is
a screen at 1:12,000 and touches 94% of Weston within 250 m. Actual approved
delineations and Conservation Commission orders of conditions would replace a
coarse multiplier with a real answer on the parcels that matter.

**6. Local historic districts and demolition-delay inventories.** MHC statewide
coverage is very uneven — 155 inventoried points against 16,944 Newton single
families — so absence proves nothing and the gate can only ever add a constraint.
Each town's own demolition-delay inventory and LHD boundaries would make the
strongest regulatory blocker in this business reliable.

**7. Title 5 / septic records and soil suitability.** Sewer status is a
service-area polygon, not a connection. Actual septic system records, perc test
results and NRCS soil suitability would replace the flat `septic_undersized`
acreage rule.

**8. Statewide building footprints, completed.** `buildingfp_poly` reached 5,673
of 31,725 parcels. Full coverage would give real lot coverage, a real existing
footprint, and a genuine setback measurement rather than a bounding-box proxy.

**9. Ownership and probate signals.** `estate` is a regex over `OWNER1` and
`care_of` is the presence of a mailing contact. Registry of Deeds and probate
filings would turn seller motivation from a proxy into an observation — subject to
checking what is lawful to use for outreach.

**10. Assessor CAMA detail.** Condition and grade codes, bathroom counts and
renovation history would sharpen functional obsolescence beyond age and size.
These live in the town CAMA system, not in the L3 extract.

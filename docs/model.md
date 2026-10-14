# Scoring and validation

These notes describe the full real-data pipeline and its historical evaluation.
The public repository bundles a separate synthetic demo, not these source records.

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
were learned from outcome data** - see [Validation](#validation) for what the
labels do and do not support, including the one assumption that *is* checked
against outcomes.

## What it does

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

## Rebuild feasibility

Multiplicative gates, each a configured multiplier in `[feasibility]`, so
several constraints compound. `0.0` is a hard blocker and forces the final
score to zero.

Unknown constraints normally do not apply a multiplier. **Lot size is the
exception:** a candidate must have both a measured parcel area and a matched
municipal minimum. Missing either one fires `lot_size_unverified` and blocks the
candidate rather than treating unknown as conforming.

Gates, and how many parcels each one fired on in the historical five-town build:

| Gate | × | Parcels | Source |
|---|---|---|---|
| `lot_size_unverified` | 0.00 | 201 | parcel area or applicable district minimum is unknown |
| `nonconforming_lot` | 0.00 | 12,974 | lot is 60–100% of the applicable zoning minimum |
| `severely_undersized_lot` | 0.00 | 5,597 | lot is below 60% of the applicable zoning minimum |
| `unbuildable_envelope` | 0.00 | 1,101 | lot × local FAR cannot reach a marketable house |
| `historic_restriction` | 0.00 | 20 | recorded preservation restriction (MHC `PR`) |
| `historic_local_district` | 0.05 | 732 | MHC area designated `LHD` |
| `wetland_overlap` | 0.10 | 1,924 | parcel intersects a MassDEP wetland |
| `flood_sfha` | 0.15 | 1,853 | mapped FEMA Special Flood Hazard Area |
| `historic_district` | 0.20 | 4,777 | MHC historic area, National Register designation |
| `septic_undersized` | 0.25 | 2,588 | off sewer and below `septic_min_acres` |
| `historic_inventory` | 0.30 | 3,110 | MHC-inventoried building on the parcel |
| `acec` | 0.30 | 0 | Area of Critical Environmental Concern |
| `narrow_lot` | 0.30 | 5,254 | bounding-box width below district frontage minimum or fallback |
| `odd_shaped_lot` | 0.35 | 228 | compactness `4πA/P²` below `min_compactness` |
| `shared_polygon` | 0.50 | 2 | several single-family records on one polygon |
| `downzoned_existing` | 0.55 | 4,556 | existing living area exceeds the local envelope |
| `wetland_buffer` | 0.55 | 1,819 | inside the 100 ft Wetlands Protection Act buffer |
| `wetland_riverfront` | 0.85 | 2,027 | within the 200 ft riverfront area |

Municipal bylaw tables match 36,095 of 36,296 parcels. Brookline, Newton and
Wellesley districts come from official GIS centroid joins; Wayland and Weston
use the district carried in the assessor extract and are marked estimated
provenance. The shipped standards use the stricter current/new-lot requirement
where a bylaw varies by lot-creation date, so this screen prefers false
negatives over clearing a legally nonconforming teardown.

The **existing house is never a floor on the envelope**. If the observed local
floor-area ratio implies something smaller than what already stands, that is the
`downzoned_existing` case and the pro forma feels it.

## Project economics

A dollar pro forma per parcel, every line item written to the parquet.

**Completed value** comes from recorded new-construction sales in the assessor's
own extract: a parcel whose `YEAR_BUILT` is recent and whose `LS_DATE` falls
within a few years of that year is a new-home sale. Public record, never
scraped, and no comparable is ever synthesised - a town without enough of them
gets a null resale value and a report line saying so.

Comparables are drawn from a nearest-neighbour search over the comp set itself,
not over all parcels: new builds are ~1.5% of these towns, so a 200-parcel
neighbourhood holds two of them. Value is then a paired-sales adjustment:

```
ARV = comparable price + marginal_sqft_share × $/sqft × (our sqft − comparable sqft)
```

bounded so the implied `$/sqft` stays inside `[0.75, 1.25] ×` the comparables'
own. Straight `$/sqft × floor area` is wrong at the small end - a new 2,000 sqft
house in Newton does not sell for 2,000 × $640, because most of the comparable's
price is land our lot also has - and unbounded the adjustment let a 2,800 sqft
envelope imply $1,283/sqft in a town whose new builds trade at $877.

Per town, from the historical build:

| Town | Comps | Own trend | Leave-one-out error | $/sqft | Observed FAR | Assess→sale |
|---|---|---|---|---|---|---|
| Brookline | 49 | 5.2% | 36.0% | $1,130 | 0.31 | 1.015 |
| Newton | 380 | 6.4% | 21.0% | $876 | 0.39 | 1.065 |
| Wayland | 36 | 8.1% | 14.7% | $570 | 0.14 | 1.098 |
| Wellesley | 250 | 7.2% | 20.4% | $903 | 0.29 | 1.067 |
| Weston | 101 | 9.3% | 32.8% | $836 | 0.09 | 0.999 |

Accuracy is reported as **leave-one-out median absolute percentage error** -
each comparable's own recorded price predicted from the others - because that is
out-of-sample error on real sales rather than the in-sample R² of a fit nobody
can check. It runs 15–36%. Treat the value as a screen, not an appraisal.

The price trend is fitted **pooled across towns** by default. Weston's own slope
fits at 9.3%/yr on 101 comps, which doubles an eight-year-old sale and produced
$7.7M resale values; and no single comparable may be time-adjusted by more than
`max_trend_adjustment`. Filtering nominal-consideration deeds matters more than
it sounds: `LS_PRICE` carries thousands of $1 and $100 family transfers, and
left in they invert the sign of the fitted trend.

**Acquisition price** is a live list price where `listings.csv` supplies one,
otherwise assessed value × the town's *measured* sale-to-assessment ratio (these
five towns land at 0.999–1.098) × an off-market premium. Where the assessor
suppresses the structure value the total assessment is depressed with it and the
multiple reads as a bargain - 1,637 Newton parcels, one of which priced out at a
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
`unprofitable_ceiling` - an unprofitable project cannot rank, but forcing 90% of
a town to exactly `0.0` would leave 30,000 tied rows and no ordering at all.

## Data confidence

Every input the score depends on is tracked as **authoritative / estimated /
missing** and reported per parcel in `prov_*` columns and in the detail panel.
Confidence is the weighted provenance share, and it *multiplies* into the final
score, so unknowns cost rank rather than being silently treated as favourable.

Historical build, share of parcels:

| Input | Authoritative | Estimated | Missing |
|---|---|---|---|
| `zoning` | 79.0% | 20.9% | 0.1% |
| `resale_value` | 90.0% | 10.0% | 0.0% |
| `lot_area` | 99.6% | 0.4% | 0.0% |
| `structure_value` | 95.3% | 0.0% | 4.7% |
| `living_area` | 79.8% | 20.2% | 0.0% |
| `acquisition_price` | 0.0% | 100.0% | 0.0% |
| `constraints` | 100.0% | 0.0% | 0.0% |
| `buildable_envelope` | 70.3% | 29.7% | 0.0% |

Acquisition price remains the largest systematic data gap: no live listings
export is loaded, so every acquisition estimate comes from assessments. Zoning
is authoritative where an official municipal GIS assignment and bylaw minimum
both exist; assessor-carried districts remain estimated even when their
dimensional standard comes from an official bylaw.

## Configuration

Everything tunable lives in `pipeline/config.toml`, grouped by concept:
`[score]`, `[feasibility]`, `[buildable]`, `[comps]`, `[acquisition]`,
`[costs]`, `[economics]`, `[obsolescence]`, `[acquisition_likelihood]`,
`[confidence]`. Change it and re-run; nothing is hardcoded, and every section
carries the reasoning for its numbers.

Local inputs are read from `pipeline/`, and the build prints which are present,
missing, or unmatched. Zoning standards ship with the repository. Parcel assignments are generated
locally with `fetch_zoning_assignments.py` and ignored by Git. Listings and
permit exports remain optional.

| File | Used by | Effect when present |
|---|---|---|
| `zoning_assignments.csv` (local only) | build | `town,loc_id,zoning,authority,source_url,as_of`. Official parcel-centroid district assignments for Brookline, Newton and Wellesley. Refresh from municipal ArcGIS layers with `uv run fetch_zoning_assignments.py` |
| `zoning_minimums.csv` | build | `town,zoning,min_lot_acres,min_frontage_ft,max_far,max_lot_coverage,authority,source_url,as_of`. Official lot-area/frontage standards for the five towns. Missing or undersized lots are hard blockers; matched FAR/coverage also makes the envelope authoritative |
| `listings.csv` | build | `town,loc_id,site_addr,status,list_price,original_list_price,list_date,days_on_market`. A real list price replaces the assessment multiple and is marked authoritative; enables the four listing components of acquisition likelihood, which are null without it |
| `permits.csv` | calibrate | `town,loc_id,site_addr,permit_date,permit_type` where `permit_type` is `demolition` or `new_dwelling`. The only direct observation of the target event. A demolition joined to a subsequent new dwelling is a success; a demolition with nothing built back is an explicit negative |

Nothing in the repository fabricates zoning, cost, permit or comparable-sale
data. Where a source is absent, the affected component is null and confidence
falls.

## Site screening

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
is very uneven - Newton has 155 inventoried points against 16,944 single
families, Wellesley 1,193 against 7,319 - so an absent MHC record is *not*
evidence a house is unlisted, and the layer can only ever add a constraint,
never clear one. Building footprints reached only 9,930 of 36,296 parcels;
statewide coverage is partial, and the columns go null rather than zero outside
it. The ACEC shapefile is dated 2009; check for newer designations before
relying on an absence.

Sanity check against local knowledge, which is how you should read any of this:
Brookline and Wellesley are 100% and Newton is 99.8% in a sewer service area;
Wayland is 0.8% and Weston is 0.6%, matching just the `WAYLAND SEWER` district
and the two school systems. Those two towns really are almost entirely septic.
No ACEC intersects the five-town build. The ACEC shapefile is dated 2009;
check for newer designations before relying on an absence.

`flag_wetland_m` at the default 250 m radius touches 94% of Weston and 89% of
Wayland, which is true but not actionable. The viewer therefore phrases the
distance by which rule it triggers: overlapping a resource area, inside the
100 ft Wetlands Protection Act buffer zone, near the 200 ft riverfront area, or
merely nearby.

## Data landmines this handles

These are the things that quietly produce wrong answers, all verified against
the real files rather than the spec:

| Trap | Handling |
|---|---|
| DBF numerics encode missing as `0`, not NULL | `0` becomes NULL for year built, areas, lot size, sale price |
| Newton reports `bldg_val = 0` on ~10% of single families that have living area and a year built | Treated as a suppressed structure value, not a worthless one. `land_ratio` and `impr_psf` go null, structure value counts as missing for confidence, **and** the acquisition estimate switches from total assessment to land value - the depressed total otherwise reads as a $356k Newton bargain. These parcels are also 5.7× more likely to be rebuilt than base rate, because `bldg_val = 0` often means the structure is *already* gone |
| `LS_PRICE` carries thousands of $1 and $100 family transfers | Filtered by `comps.min_price` everywhere a sale is used. Left in they invert the sign of the fitted price trend |
| `BLD_AREA` is gross area and `RES_AREA` is living area, differing by ~60% | Demolition cost uses gross; the envelope comparison uses living, because the envelope is inferred from the living area of new builds. Mixing them flagged most of Newton as downzoned |
| Use codes are 4 characters (`1010`, `1013`, `930V`), not 3 | Class is `left(code, 3)`, cross-checked against each town's `UC_LUT` |
| `LOT_SIZE` unit varies by row, carried in `LOT_UNITS` | Normalised to acres |
| `LS_DATE` is `C(8)` text; 226 rows are the sentinel `19000101` | Parsed, and anything before 1950 dropped |
| Newton's `CITY` holds village names while `OWN_CITY` says `NEWTON` | Absentee compares street address and state, never city |
| One polygon can carry hundreds of assessment rows | Condo and exempt filtering is applied at the polygon level, so a single condo record disqualifies the whole parcel |
| Wellesley ships `BLD_AREA` and assessor `ZONING` entirely empty | `far` falls back to living area; zoning is assigned from Wellesley's official GIS district polygons |
| `LOC_ID` is `F_<x>_<y>`, US survey feet, while geometry is metres | Treated as an opaque key; coordinates never parsed out of it |

`pipeline/artifacts/parcels_completeness.csv` reports per-town coverage for every
component, measured after these corrections, so it shows what scoring actually
saw.

## Validation

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

### What counts as success

"Was it demolished" is **not** the target. A demolition with no replacement house
is a failure and is counted as a negative.

| Label | Definition | Counted as success |
|---|---|---|
| `rebuild` | `YEAR_BUILT` advanced ≥15 years **and** the structure changed (living area moved ≥10% or building value at least doubled) | yes |
| `rebuild_sold` | `rebuild`, and the finished house then sold as new construction. **Default target** | yes |
| `viable` | `rebuild_sold`, and the recorded sale price cleared the modelled total project cost | yes |
| `permits` | demolition permit joined to a subsequent new-dwelling permit | yes, when `permits.csv` exists |
| `demo_only` | demolition permit with nothing built back | **explicit negative**, reported separately |
| `value_jump` | building value grew 3× faster than the town median | **no** - mostly large renovations, reported for reference only |

The structure-changed half of `rebuild` matters: an assessor correcting a wrong
`YEAR_BUILT` is not a rebuild. And the resale test needs two conditions, because
`LS_PRICE` holds only the *last* sale: it must be dated at or after the build
year **and** priced like a finished house (≥85% of the new total assessment).
Without the second test, builders' land purchases pass as finished-home sales -
one example property's last recorded sale is an $880k lot purchase in the same year its
new house was finished.

The deepest available five-town validation window averages 5.0 fiscal years:
`rebuild` 434, `rebuild_sold` 208, `viable` 128. Historical `LOC_ID` changes
mean the oldest ranking join covers Newton, Wellesley and Weston; shorter
splits include all five towns.

### The one assumption checked against outcomes

For every parcel that was rebuilt and sold, the resale price and the built floor
area are both observed. Solving the pro forma for the construction rate that
would have made each of those projects break even gives a distribution:

| Town | n | Break-even $/sqft (p25 / median / p75) | At a 20% margin |
|---|---|---|---|
| Brookline | 23 | $413 / $553 / $640 | $374 |
| Newton | 103 | $308 / $374 / $427 | $234 |
| Wayland | 7 | $169 / $183 / $253 | $103 |
| Wellesley | 55 | $302 / $405 / $457 | $247 |
| Weston | 20 | $255 / $362 / $469 | $227 |
| **All** | **208** | **$297 / $384 / $462** | **$243** |

`costs.construction_per_sqft` remains set to **$350**, slightly below the current
$384 observed median. Every run reprints this table and warns when the configured
rate sits above the median. This is a calibration check against outcomes, not a
fitted weight or a substitute for a builder's actual cost schedule.

### Ranking performance, and an honest negative result

Time-ordered: the model is scored on a prior vintage and outcomes read from the
current release, with the caveats about already-started projects described below. `--fit` splits further, training on the older
vintages and validating on the most recent prior one.

Deepest available window, `rebuild_sold`, 178 successes in 27,475 parcels
(0.65% base):

| Ranking by | AUC | P@50 | P@100 | P@250 | recall@250 |
|---|---|---|---|---|---|
| `rebuild_opportunity_score` (this model) | 0.601 | 2.0% | 1.0% | 0.8% | 1.1% |
| `structure_obsolescence_score` (the old model) | **0.840** | **8.0%** | **8.0%** | **6.0%** | 8.4% |
| `project_economics_score` alone | 0.649 | 0.0% | 0.0% | 0.8% | 1.1% |
| `rebuild_feasibility_score` alone | 0.587 | 0.0% | 0.0% | 0.4% | 0.6% |
| `acquisition_likelihood_score` alone | 0.495 | 0.0% | 1.0% | 0.4% | 0.6% |

**The obsolescence-only ranking predicts observed rebuilds substantially better
than the economics-driven one.** That result held after fixing four real
modelling bugs, and remains a limitation of the current ranking. Profitable deal selection
has not been established by these results. Three things are going on:

1. **Part of the old score's edge is leakage.** Parcels whose structure value is
   suppressed are disproportionately likely to be rebuilt because `bldg_val =
   0` frequently means the house is already gone. Excluding parcels that are no
   longer old reduces that edge, but the old model still wins.
2. **The label measures occurrence, not quality.** "Which cheap obsolete houses
   did builders buy" is largely a demand signal, and obsolescence measures it
   directly. A genuinely excellent site that nobody has got to yet counts as a
   negative, and the model is explicitly built to surface those.
3. **The economics is a noisy multiplier.** Resale value carries 15–36%
   out-of-sample error and acquisition price is modelled, not observed. A margin
   built from two noisy estimates has a tail dominated by estimation error, and
   `precision@50` lives entirely in that tail.

Every run prints a sensitivity sweep of the two levers that decide which side
wins, so the trade-off is measured rather than argued. Deepest window:

```
economics_exponent    0  AUC 0.589 P@100 0.0%   0.25  AUC 0.596 P@100 1.0%   1* AUC 0.601 P@100 1.0%   1.5  AUC 0.601 P@100 1.0%
support_strength      0  AUC 0.600 P@100 1.0%   0.35* AUC 0.601 P@100 1.0%   1  AUC 0.601 P@100 1.0%   4  AUC 0.603 P@100 1.0%
obsolescence only        AUC 0.840 P@100 8.0%                                             (* = configured)
```

`--fit` grid-searches 480 combinations of the combination weights on the training
split and reports both splits. It **refuses to fit below 150 training successes**
rather than dressing up a sparse fit as optimisation. The newest five-town split
currently has only 38 successes, so fitted settings should be treated cautiously
until more outcomes accrue.

The defaults remain configured expert settings, not retuned
to win this metric, for the reason in point 2: the brief is economically viable
projects, not demolition occurrence. If you would rather rank on the empirically
stronger occurrence signal, lower `score.economics_exponent` toward 0 and raise
`score.support_strength` - the numbers above tell you what that costs and buys.

Longer horizons matter more than anything else: a candidate that has not been
rebuilt *yet* counts as a negative, so short windows understate precision
severely. Results are reported separately by town on every run.

## What the model still needs

Ranked by how much each one would improve the answer, not by how easy it is to
get. The first three are where the model is currently guessing hardest, and all
three are public records.

**1. Full buildable envelopes and parcel-specific rebuild rights.** Minimum lot
area is now enforced as a hard eligibility rule, and official district geometry
is joined for Brookline, Newton and Wellesley. The model also uses bylaw FAR or coverage
limits where the source table supplies them. Remaining work: authoritative
Wayland and Weston district geometry, lot-creation dates, setbacks, height,
variances, and determinations of grandfathered status. Until those facts are
verified, an unmatched or undersized lot is blocked rather than treated as a
plausible teardown.

**2. Building permits - demolition joined to new dwelling.** The only direct
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
listings are the whole timing half of acquisition likelihood - all null today.
Closed new-construction sales would also replace the assessor's single
`LS_PRICE`/`LS_DATE` per parcel, which is the reason the comparable set is only
~100 sales per town and the resale model carries 15–36% error. Source: your MLS
via an authorised RESO Web API feed. Load into `pipeline/listings.csv`.

**4. A builder's actual cost schedule.** `costs.construction_per_sqft` is
currently checked against 208 real completed-and-sold projects; its $350 setting
is slightly below that window's $384 median and is a sanity check rather than a
quote. Real numbers per square foot by product type,
plus site work, demolition, abatement and septic, would move the pro forma from
plausible to usable. No dataset needed - one conversation.

**5. Wetland delineations and conservation decisions.** The DEP wetlands layer is
a screen at 1:12,000 and touches 94% of Weston within 250 m. Actual approved
delineations and Conservation Commission orders of conditions would replace a
coarse multiplier with a real answer on the parcels that matter.

**6. Local historic districts and demolition-delay inventories.** MHC statewide
coverage is very uneven - 155 inventoried points against 16,944 Newton single
families - so absence proves nothing and the gate can only ever add a constraint.
Each town's own demolition-delay inventory and LHD boundaries would make the
strongest regulatory blocker in this business reliable.

**7. Title 5 / septic records and soil suitability.** Sewer status is a
service-area polygon, not a connection. Actual septic system records, perc test
results and NRCS soil suitability would replace the flat `septic_undersized`
acreage rule.

**8. Statewide building footprints, completed.** `buildingfp_poly` reached 9,930
of 36,296 parcels. Full coverage would give real lot coverage, a real existing
footprint, and a genuine setback measurement rather than a bounding-box proxy.

**9. Ownership and probate signals.** `estate` is a regex over `OWNER1` and
`care_of` is the presence of a mailing contact. Registry of Deeds and probate
filings would turn seller motivation from a proxy into an observation - subject to
checking what is lawful to use for outreach.

**10. Assessor CAMA detail.** Condition and grade codes, bathroom counts and
renovation history would sharpen functional obsolescence beyond age and size.
These live in the town CAMA system, not in the L3 extract.

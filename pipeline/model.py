#!/usr/bin/env python3
"""Teardown-to-rebuild opportunity model.

Pure numpy. Nothing here opens a file or a database: every function takes arrays
and configuration and returns arrays, so `build.py` can score the current
release, `calibrate.py` can re-score a 2016 vintage, and `--selfcheck` can score
six hand-written rows.

Five sub-scores, deliberately separate:

  rebuild_feasibility_score     can a replacement house be built at all
  project_economics_score       does the completed project pencil, in dollars
  acquisition_likelihood_score  will it trade, and when
  structure_obsolescence_score  is keeping the existing house economically weak
  data_confidence_score         how much of the above is actually known

and one ranking:

  rebuild_opportunity_score = feas^a * econ^b * conf^c * support

The core is multiplicative. A hard feasibility failure is a zero, not a
deduction, so nothing else can promote a parcel that cannot be rebuilt. The
supporting term spans [1, 1+support_strength] and only re-orders parcels that
already clear the core.

Provenance is tracked per input as AUTHORITATIVE / ESTIMATED / MISSING and
reported, not hidden: no component is ever imputed to a favourable value.
"""

import numpy as np

MISSING, ESTIMATED, AUTHORITATIVE = 0, 1, 2
PROVENANCE_NAMES = {MISSING: "missing", ESTIMATED: "estimated",
                    AUTHORITATIVE: "authoritative"}

SQ_FT_PER_ACRE = 43560.0

# Structure-obsolescence components and the direction that means "the existing
# house is economically weak". These were the entire score before; they are one
# supporting sub-score now.
OBSOLESCENCE = {
    "land_ratio": "hi",
    "impr_psf": "lo",
    "age_fit": "hi",
    "far": "lo",
    "area_vs_nbhd": "lo",
}

# Acquisition-likelihood components. `None` means the value is already 0/1 and
# is used directly rather than percentile-ranked.
ACQUISITION = {
    "years_since_sale": "hi",
    "absentee": None,
    "estate": None,
    "care_of": None,
    "listed": None,
    "days_on_market": "hi",
    "price_cut": None,
    "withdrawn_or_expired": None,
}

# Which raw column each ranked component's per-town coverage is judged on.
SOURCE_FIELD = {
    "land_ratio": "land_ratio",
    "impr_psf": "impr_psf",
    "age_fit": "year_built",
    "far": "far",
    "area_vs_nbhd": "res_area",
    "years_since_sale": "years_since_sale",
    "absentee": "own_addr",
    "estate": "owner1",
    "care_of": "owner1",
    "listed": "listing_status",
    "days_on_market": "days_on_market",
    "price_cut": "days_on_market",
    "withdrawn_or_expired": "listing_status",
}

# Feasibility gate -> the config key holding its multiplier. Order is the order
# the notes column lists them in; it does not affect the product.
GATES = [
    "wetland_overlap", "wetland_buffer", "wetland_riverfront", "flood_sfha",
    "acec", "historic_inventory", "historic_district",
    "historic_local_district", "historic_restriction", "nonconforming_lot",
    "severely_undersized_lot", "septic_undersized", "odd_shaped_lot",
    "narrow_lot", "downzoned_existing", "shared_polygon",
    "unbuildable_envelope",
]

CONFIDENCE_INPUTS = [
    "zoning", "resale_value", "lot_area", "structure_value", "living_area",
    "acquisition_price", "constraints", "buildable_envelope",
]


# --------------------------------------------------------------------------
# ranking primitives
# --------------------------------------------------------------------------

def age_fit(year: np.ndarray, cfg) -> np.ndarray:
    """Raw age signal, higher means more functionally obsolete.

    Band mode: 1.0 inside [peak_lo, peak_hi], decaying linearly to 0 over
    falloff_years on either side. Pre-1900 in MetroWest correlates with
    demolition delay applying, i.e. with the deal being blocked, so
    "older is always better" is the wrong shape.
    """
    a = cfg["age"]
    if a["mode"] == "older_better":
        return -year
    lo, hi, fall = a["peak_lo"], a["peak_hi"], float(a["falloff_years"])
    with np.errstate(invalid="ignore"):
        below = np.clip(1 - (lo - year) / fall, 0, 1)
        above = np.clip(1 - (year - hi) / fall, 0, 1)
        band = np.where(year < lo, below, np.where(year > hi, above, 1.0))
    return np.where(np.isnan(year), np.nan, band)


def pct_rank(values: np.ndarray, groups: np.ndarray, direction: str) -> np.ndarray:
    """Percentile rank within group, ignoring NaN. 1.0 = strongest signal."""
    out = np.full(values.shape, np.nan)
    v = values if direction == "hi" else -values
    for g in np.unique(groups):
        m = (groups == g) & ~np.isnan(v)
        n = int(m.sum())
        if n == 0:
            continue
        if n == 1:
            out[m] = 0.5
            continue
        order = np.argsort(v[m], kind="stable")
        r = np.empty(n)
        r[order] = np.arange(n)
        out[m] = r / (n - 1)
    return out


def nbr_pct_rank(values: np.ndarray, idx: np.ndarray, direction: str) -> np.ndarray:
    """Percentile of each parcel among its k nearest neighbours.

    ponytail: dense (n, k) comparison, O(n*k). Fine to ~10^5 parcels at k=200
    (~10^7 float compares). For the whole state, chunk it by town.
    """
    v = values if direction == "hi" else -values
    nb = v[idx]
    with np.errstate(invalid="ignore"):
        lower = np.sum(nb < v[:, None], axis=1)
    cnt = np.sum(~np.isnan(nb), axis=1)
    out = np.where(cnt > 1, lower / np.maximum(cnt - 1, 1), 0.5)
    return np.where(np.isnan(v), np.nan, out)


def knn_index(towns: np.ndarray, xy: np.ndarray, k: int) -> np.ndarray:
    """(n, k) indices of the k nearest same-town parcels, in 26986 metres."""
    from scipy.spatial import cKDTree
    n = len(towns)
    idx = np.zeros((n, k), dtype=np.int64)
    for t in np.unique(towns):
        m = np.flatnonzero(towns == t)
        kk = min(k, len(m))
        _, sub = cKDTree(xy[m]).query(xy[m], k=kk)
        sub = np.atleast_2d(sub).reshape(len(m), kk)
        idx[m, :kk] = m[sub]
        if kk < k:                        # tiny town: pad by repeating self
            idx[m, kk:] = m[sub[:, :1]]
    return idx


def comp_neighbors(xy: np.ndarray, comp_mask: np.ndarray, towns: np.ndarray,
                   count: int, radius_m: float):
    """(n, count) indices into the comp set, and their distances.

    Separate from knn_index because comparable new builds are ~1.5% of these
    towns: the k = 200 parcel neighbourhood holds two or three of them, which is
    not a comparable set. Searching the comp set directly guarantees `count`
    of them, and the distance is returned so a caller can reject a
    "neighbourhood" that is actually two towns wide.
    """
    from scipy.spatial import cKDTree
    n = len(towns)
    idx = np.full((n, count), -1, dtype=np.int64)
    dist = np.full((n, count), np.inf)
    for t in np.unique(towns):
        rows = np.flatnonzero(towns == t)
        comps = np.flatnonzero(comp_mask & (towns == t))
        if len(comps) == 0:
            continue
        kk = min(count, len(comps))
        d, sub = cKDTree(xy[comps]).query(xy[rows], k=kk)
        d = np.atleast_2d(d).reshape(len(rows), kk)
        sub = np.atleast_2d(sub).reshape(len(rows), kk)
        idx[np.ix_(rows, np.arange(kk))] = comps[sub]
        dist[np.ix_(rows, np.arange(kk))] = d
    return idx, np.where(dist > radius_m, np.inf, dist)


def _masked_median(values: np.ndarray, idx: np.ndarray, dist: np.ndarray):
    """Median of `values[idx]` over the entries with a finite distance."""
    v = np.where(np.isfinite(dist), values[np.clip(idx, 0, None)], np.nan)
    v = np.where(idx < 0, np.nan, v)
    cnt = np.sum(~np.isnan(v), axis=1)
    med = np.full(len(v), np.nan)
    # nanmedian warns on an all-NaN row, and a parcel with no comparable in
    # range is the normal case rather than an error.
    any_ = cnt > 0
    if any_.any():
        with np.errstate(invalid="ignore"):
            med[any_] = np.nanmedian(v[any_], axis=1)
    return med, cnt


# --------------------------------------------------------------------------
# comparable sales: price trend and the resale-value model
# --------------------------------------------------------------------------

def fit_price_trend(sale_year: np.ndarray, ppsf: np.ndarray, cfg) -> float:
    """Annual log price trend, clamped. Returns 0.0 when it cannot be fitted.

    Fitted on every arms-length recorded sale in the town, not just new builds,
    because the trend needs volume and the level does not come from here. The
    caller must have filtered nominal-consideration deeds: a $1 transfer between
    family members is a real row in LS_PRICE and enough of them will invert the
    sign of this slope.
    """
    c = cfg["comps"]
    ok = np.isfinite(sale_year) & np.isfinite(ppsf) & (ppsf > 0)
    if ok.sum() < c["min_comps_town"] or len(np.unique(sale_year[ok])) < 3:
        return 0.0
    x = sale_year[ok] - sale_year[ok].mean()
    slope = np.polyfit(x, np.log(ppsf[ok]), 1)[0]
    return float(np.clip(slope, c["min_annual_trend"], c["max_annual_trend"]))


def resale_value(buildable_sqft: np.ndarray, comp_price: np.ndarray,
                 comp_sqft: np.ndarray, comp_ppsf: np.ndarray, cfg):
    """After-rebuild value by paired-sales adjustment on new-build comparables.

        ARV = comparable price + marginal_rate x (our sqft - comparable sqft)

    which is what an appraiser does and what a builder underwrites. Straight
    $/sqft x floor area is wrong at the small end: a new 2,000 sqft house in
    Newton does not sell for 2,000 x $640, because most of the price is land
    that is already in the comparable. The marginal rate is therefore a fraction
    of average $/sqft, config `comps.marginal_sqft_share`.

    NaN in, NaN out. A parcel with no comparable set gets no resale value.
    """
    c = cfg["comps"]
    rate = c["marginal_sqft_share"] * comp_ppsf
    with np.errstate(invalid="ignore"):
        arv = comp_price + rate * (buildable_sqft - comp_sqft)
        # Then bound the IMPLIED $/sqft against the comparables' own. Smaller
        # houses do sell at a higher $/sqft, because the land underneath is
        # fixed, but only so much: without this bound a 2,800 sqft envelope
        # inherits enough of a 4,000 sqft comparable to imply $1,283/sqft in a
        # town whose new builds trade at $877, and those fictional margins land
        # at the top of the ranking.
        ppsf = arv / buildable_sqft
        bounded = np.clip(ppsf, c["min_implied_ppsf_share"] * comp_ppsf,
                          c["max_implied_ppsf_share"] * comp_ppsf)
        arv = bounded * buildable_sqft
        return np.where(arv > 0, arv, np.nan)


def loo_mape(price: np.ndarray, sqft: np.ndarray, cfg) -> float:
    """Leave-one-out median absolute percentage error of `resale_value`.

    Predicts each comparable's own recorded sale price from the others, so the
    number reported for a town is out-of-sample accuracy on real sales rather
    than the in-sample R2 of a fit nobody can check.
    """
    ok = np.isfinite(price) & np.isfinite(sqft) & (sqft > 0) & (price > 0)
    p, s = price[ok], sqft[ok]
    n = len(p)
    if n < max(3, cfg["comps"]["min_comps_town"]):
        return float("nan")
    tot_p, tot_s = p.sum(), s.sum()
    ppsf = p / s
    tot_ppsf = ppsf.sum()
    # Leave-one-out means of the other n-1 comparables, without an n x n matrix.
    med_p = (tot_p - p) / (n - 1)
    med_s = (tot_s - s) / (n - 1)
    med_ppsf = (tot_ppsf - ppsf) / (n - 1)
    pred = resale_value(s, med_p, med_s, med_ppsf, cfg)
    with np.errstate(invalid="ignore"):
        return float(np.nanmedian(np.abs(pred - p) / p))


# --------------------------------------------------------------------------
# buildable envelope
# --------------------------------------------------------------------------

def buildable_envelope(d, cfg, observed_far: np.ndarray,
                       market_ceiling: np.ndarray):
    """Plausible replacement-house floor area, and where the number came from.

    Preference order:
      1. max_far from the zoning table            -> authoritative
      2. max_lot_coverage x storeys from zoning    -> authoritative
      3. floor area ratio of new builds nearby     -> estimated
    Never the existing house: if the local envelope is smaller than what stands
    today, that is the downzoned case and the pro forma should feel it.
    """
    b = cfg["buildable"]
    lot_sqft = d["lot_acres"] * SQ_FT_PER_ACRE
    far = np.where(np.isfinite(d["max_far"]), d["max_far"], np.nan)
    src = np.where(np.isfinite(far), AUTHORITATIVE, MISSING)

    cover = d["max_lot_coverage"]
    use_cover = ~np.isfinite(far) & np.isfinite(cover)
    # Two storeys of the allowed footprint is the conventional reading of a
    # coverage-only bylaw for a single family in these districts.
    far = np.where(use_cover, cover * 2.0, far)
    src = np.where(use_cover, AUTHORITATIVE, src)

    use_obs = ~np.isfinite(far) & np.isfinite(observed_far)
    far = np.where(use_obs, observed_far, far)
    src = np.where(use_obs, ESTIMATED, src)

    with np.errstate(invalid="ignore"):
        raw = far * lot_sqft
        ceiling = np.fmin(np.where(np.isfinite(market_ceiling), market_ceiling,
                                   b["max_home_sqft"]), b["max_home_sqft"])
        sized = np.clip(raw, None, ceiling)
    return raw, sized, src


# --------------------------------------------------------------------------
# feasibility
# --------------------------------------------------------------------------

def feasibility(d, cfg, envelope_raw: np.ndarray, buildable_sqft: np.ndarray):
    """Multiplicative gates. Returns (score, notes, gate masks).

    An unknown never applies a multiplier. A parcel whose zoning minimum is
    unknown is not penalised for being nonconforming and is not credited with
    conforming either; it loses confidence, which is the only honest treatment.
    """
    f = cfg["feasibility"]
    n = len(buildable_sqft)
    # Living area, NOT gross building area: the envelope is inferred from the
    # living area of new builds, and BLD_AREA counts basement and garage, so
    # comparing the two would flag most of Newton as downzoned.
    existing = d["existing_living_sqft"]

    wet = d["flag_wetland_m"]
    lot = d["lot_acres"]
    minlot = d["min_lot_acres"]
    sewer = d["flag_sewer"]

    def yes(a):
        return np.where(np.isnan(a) if a.dtype.kind == "f" else False, False, a)

    with np.errstate(invalid="ignore"):
        masks = {
            "wetland_overlap": np.isfinite(wet) & (wet <= 0),
            # 100 ft Wetlands Protection Act buffer zone, in metres.
            "wetland_buffer": np.isfinite(wet) & (wet > 0) & (wet <= 30.5),
            "wetland_riverfront": np.isfinite(wet) & (wet > 30.5) & (wet <= 61.0),
            "flood_sfha": d["flag_flood_sfha"],
            "acec": d["flag_acec_present"],
            "historic_inventory": d["flag_historic_inventory"],
            "historic_district": d["flag_historic_district"],
            "historic_local_district": d["flag_historic_local_district"],
            "historic_restriction": d["flag_historic_restriction"],
            "nonconforming_lot": (np.isfinite(minlot) & np.isfinite(lot)
                                  & (lot < minlot)
                                  & (lot >= f["severely_undersized_fraction"] * minlot)),
            "severely_undersized_lot": (
                np.isfinite(minlot) & np.isfinite(lot)
                & (lot < f["severely_undersized_fraction"] * minlot)),
            # Sewer unknown is not treated as septic here: the cost model
            # assumes septic (conservative on dollars) but a gate that fires on
            # an unknown would be asserting a constraint nobody observed.
            "septic_undersized": ((sewer == 0) & np.isfinite(lot)
                                  & (lot < f["septic_min_acres"])),
            "odd_shaped_lot": (np.isfinite(d["lot_compactness"])
                               & (d["lot_compactness"] < f["min_compactness"])),
            # The district's own frontage minimum where the zoning table
            # supplies one, the configured fallback otherwise. Bounding-box
            # width is a proxy for frontage and not frontage: a corner lot or a
            # diagonal street makes the box wider than the street line, so this
            # under-fires rather than over-fires.
            "narrow_lot": (np.isfinite(d["lot_width_ft"])
                           & (d["lot_width_ft"]
                              < np.where(np.isfinite(d["min_frontage_ft"]),
                                         d["min_frontage_ft"],
                                         f["min_lot_width_ft"]))),
            "downzoned_existing": (np.isfinite(envelope_raw) & np.isfinite(existing)
                                   & (existing > envelope_raw * 1.02)),
            "shared_polygon": d["n_records_on_parcel"] > 1,
            "unbuildable_envelope": (np.isfinite(envelope_raw)
                                     & (envelope_raw < cfg["buildable"]["min_viable_sqft"])),
        }

    score = np.ones(n)
    notes = np.array([""] * n, dtype=object)
    for g in GATES:
        m = np.asarray(masks[g], dtype=bool)
        score = np.where(m, score * float(f[g]), score)
        notes[m] = np.where(notes[m] == "", g, notes[m] + "|" + g)
    return np.clip(score, 0.0, 1.0), notes, masks


# --------------------------------------------------------------------------
# economics
# --------------------------------------------------------------------------

def pro_forma(d, cfg, arv: np.ndarray, buildable_sqft: np.ndarray,
              acq: np.ndarray):
    """Full-project dollar pro forma. Every line item is an output column.

    Returns a dict of arrays. NaN propagates: a parcel with no resale value gets
    no profit, not a zero profit.
    """
    c = cfg["costs"]
    existing = np.where(np.isfinite(d["existing_sqft"]), d["existing_sqft"], np.nan)

    demo = np.fmax(c["demolition_per_sqft"] * existing, c["demolition_minimum"])
    demo = np.where(np.isfinite(demo), demo, c["demolition_minimum"])
    pre78 = np.isfinite(d["year_built"]) & (d["year_built"] < c["abatement_year_cutoff"])
    demo = demo + np.where(pre78, c["abatement"], 0.0)

    # Sewer unknown -> assume a new septic system. Conservative on cost, and the
    # provenance column says the input was estimated.
    on_sewer = d["flag_sewer"] == 1
    utilities = np.where(on_sewer, c["utility_connections"],
                         c["septic_system"] + c["utility_connections"])

    hard = c["construction_per_sqft"] * buildable_sqft + c["site_work"] + utilities
    soft = hard * (c["design_permitting_pct"] + c["contingency_pct"])
    buy_costs = acq * c["buyer_closing_pct"]

    basis = acq + buy_costs + demo + hard + soft
    months = float(c["carry_months"])
    interest = basis * c["finance_ltc"] * c["annual_finance_rate"] * months / 12.0
    taxes = acq * c["property_tax_rate"] * months / 12.0
    insurance = c["insurance_annual"] * months / 12.0
    carry = interest + taxes + insurance

    sale_costs = arv * (c["brokerage_pct"] + c["seller_closing_pct"]
                        + c["transfer_stamps_pct"])

    total = basis + carry + sale_costs
    profit = arv - total
    with np.errstate(invalid="ignore", divide="ignore"):
        margin = profit / arv
        roc = profit / total
    return {
        "acq_price": acq, "demo_cost": demo, "hard_cost": hard,
        "soft_cost": soft, "carry_cost": carry, "sale_cost": sale_costs,
        "buy_cost": buy_costs, "total_cost": total, "arv": arv,
        "profit": profit, "margin": margin, "return_on_cost": roc,
    }


def implied_construction_cost(d, cfg, arv, sqft, acq, target_margin=0.0):
    """The $/sqft at which each project would hit `target_margin`, exactly.

    Every cost line is either fixed or linear in construction_per_sqft, and so
    is the carry that sits on top of it, so profit is linear in the rate and two
    evaluations pin it. Run over projects that were actually built and sold, this
    is how the biggest assumption in config.toml gets checked against outcomes
    instead of against opinion.
    """
    def margin_at(rate):
        cfg2 = {**cfg, "costs": {**cfg["costs"], "construction_per_sqft": rate}}
        return pro_forma(d, cfg2, arv, sqft, acq)["margin"]

    lo, hi = 100.0, 600.0
    m_lo, m_hi = margin_at(lo), margin_at(hi)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = (m_hi - m_lo) / (hi - lo)
        rate = lo + (target_margin - m_lo) / slope
        return np.where(np.isfinite(rate) & (slope < 0), rate, np.nan)


def sensitivity(d, cfg, arv, buildable_sqft, acq):
    """Margin under each adverse move, and under all three at once."""
    e = cfg["economics"]
    out = {}
    for name, kw in [
        ("acq", dict(acq=acq * (1 + e["sensitivity_acquisition"]))),
        ("cost", dict(bump=1 + e["sensitivity_cost"])),
        ("value", dict(arv=arv * (1 - e["sensitivity_value"]))),
        ("all", dict(acq=acq * (1 + e["sensitivity_acquisition"]),
                     bump=1 + e["sensitivity_cost"],
                     arv=arv * (1 - e["sensitivity_value"]))),
    ]:
        bump = kw.pop("bump", 1.0)
        cfg2 = cfg
        if bump != 1.0:
            cfg2 = dict(cfg)
            cfg2["costs"] = {k: (v * bump if k in (
                "demolition_per_sqft", "demolition_minimum", "abatement",
                "construction_per_sqft", "site_work", "septic_system",
                "utility_connections") else v)
                for k, v in cfg["costs"].items()}
        pf = pro_forma(d, cfg2, kw.get("arv", arv), buildable_sqft,
                       kw.get("acq", acq))
        out[f"margin_if_{name}"] = pf["margin"]
    return out


def economics_score(margin: np.ndarray, margin_downside: np.ndarray,
                    subdivision_lots: np.ndarray, cfg) -> np.ndarray:
    """Margin -> 0..1.

    At or above target_margin, 1.0. Between min_margin and target, linear. Below
    min_margin the score drops onto an exponential tail capped at
    `unprofitable_ceiling`: an unprofitable project cannot rank, but 90% of a
    town collapsing to exactly 0.0 would leave the map and the table with 30,000
    tied rows and no ordering at all.
    """
    e = cfg["economics"]
    lo, hi = e["min_margin"], e["target_margin"]
    ceiling = e.get("unprofitable_ceiling", 0.0)
    span = e.get("tail_margin_span", 0.15)
    with np.errstate(invalid="ignore"):
        blend = ((1 - e["downside_weight"]) * margin
                 + e["downside_weight"] * np.where(np.isfinite(margin_downside),
                                                   margin_downside, margin))
        s = np.clip((blend - lo) / (hi - lo), 0.0, 1.0)
        if ceiling > 0:
            tail = ceiling * np.exp((blend - lo) / span)
            s = np.where(blend < lo, np.minimum(tail, ceiling), s)
        s = np.where(np.isfinite(blend), s, np.nan)
        # Bounded upside for a lot that could plausibly be split. Deliberately
        # not a second pro forma: a real subdivision needs frontage, a road and
        # a planning board, none of which are in this dataset.
        split = np.isfinite(subdivision_lots) & (subdivision_lots >= 2)
        s = np.where(split, np.clip(s * (1 + e["subdivision_bonus"]), 0, 1), s)
    return s


# --------------------------------------------------------------------------
# sub-score assembly
# --------------------------------------------------------------------------

def weighted_subscore(parts: dict[str, np.ndarray], weights: dict[str, float]):
    """Weighted mean over the components that have data.

    Weight redistributes WITHIN this sub-score only, and the surviving share is
    returned so the caller can fold it into confidence rather than pretend a
    one-component score is as good as a five-component one.
    """
    n = len(next(iter(parts.values())))
    num, den = np.zeros(n), np.zeros(n)
    for k, v in parts.items():
        w = float(weights.get(k, 0.0))
        if w == 0.0:
            continue
        ok = np.isfinite(v)
        num[ok] += w * v[ok]
        den[ok] += w
    total = sum(float(weights.get(k, 0.0)) for k in parts)
    with np.errstate(invalid="ignore"):
        score = np.where(den > 0, num / den, np.nan)
    return score, (den / total if total else np.zeros(n))


def confidence_score(prov: dict[str, np.ndarray], cfg) -> np.ndarray:
    c = cfg["confidence"]
    n = len(next(iter(prov.values())))
    total = sum(float(c[k]) for k in CONFIDENCE_INPUTS)
    credit = np.zeros(n)
    for k in CONFIDENCE_INPUTS:
        p = prov[k]
        credit += float(c[k]) * np.where(p == AUTHORITATIVE, 1.0,
                                         np.where(p == ESTIMATED,
                                                  c["estimated_credit"], 0.0))
    return np.clip(credit / total, c["floor"], 1.0)


def combine(feas, econ, conf, acq_like, obsolescence, cfg):
    """feas^a * econ^b * conf^c * bounded support term."""
    s = cfg["score"]
    wa, wo = s["support_acquisition_likelihood"], s["support_structure_obsolescence"]
    with np.errstate(invalid="ignore"):
        # A missing supporting sub-score contributes its neutral 0.5 rather than
        # dragging the parcel down for a signal nobody measured.
        a = np.where(np.isfinite(acq_like), acq_like, 0.5)
        o = np.where(np.isfinite(obsolescence), obsolescence, 0.5)
        support = (wa * a + wo * o) / (wa + wo)
        core = (np.power(np.clip(feas, 0, 1), s["feasibility_exponent"])
                * np.power(np.clip(econ, 0, 1), s["economics_exponent"])
                * np.power(np.clip(conf, 0, 1), s["confidence_exponent"]))
        strength = s["support_strength"]
        return core * (1 + strength * support) / (1 + strength)


# --------------------------------------------------------------------------
# selfcheck
# --------------------------------------------------------------------------

def _cfg():
    """Minimal config for the selfcheck, mirroring config.toml's shape."""
    return {
        "age": {"mode": "band", "peak_lo": 1945, "peak_hi": 1975,
                "falloff_years": 35},
        "score": {"feasibility_exponent": 1.0, "economics_exponent": 1.0,
                  "confidence_exponent": 0.5, "support_strength": 0.35,
                  "support_acquisition_likelihood": 0.6,
                  "support_structure_obsolescence": 0.4},
        "confidence": {"zoning": 0.22, "resale_value": 0.20, "lot_area": 0.12,
                       "structure_value": 0.10, "living_area": 0.08,
                       "acquisition_price": 0.12, "constraints": 0.10,
                       "buildable_envelope": 0.06, "estimated_credit": 0.55,
                       "floor": 0.15},
        "economics": {"target_margin": 0.20, "min_margin": 0.02,
                      "downside_weight": 0.4, "subdivision_bonus": 0.10,
                      "sensitivity_acquisition": 0.1, "sensitivity_cost": 0.12,
                      "sensitivity_value": 0.08, "subdivision_min_multiple": 2.0},
        "comps": {"min_comps_town": 5, "min_annual_trend": -0.05,
                  "max_annual_trend": 0.12},
        "buildable": {"min_viable_sqft": 1800.0, "max_home_sqft": 7500.0},
        "feasibility": {g: 0.5 for g in GATES} | {
            "wetland_overlap": 0.10, "unbuildable_envelope": 0.0,
            "severely_undersized_fraction": 0.6, "septic_min_acres": 0.75,
            "min_compactness": 0.30, "min_lot_width_ft": 55.0},
        "costs": {"demolition_per_sqft": 12.0, "demolition_minimum": 25000.0,
                  "abatement": 18000.0, "abatement_year_cutoff": 1978,
                  "construction_per_sqft": 425.0, "site_work": 60000.0,
                  "septic_system": 45000.0, "utility_connections": 15000.0,
                  "design_permitting_pct": 0.07, "contingency_pct": 0.08,
                  "carry_months": 14, "annual_finance_rate": 0.10,
                  "finance_ltc": 0.70, "property_tax_rate": 0.0105,
                  "insurance_annual": 4000.0, "brokerage_pct": 0.045,
                  "seller_closing_pct": 0.01, "transfer_stamps_pct": 0.00456,
                  "buyer_closing_pct": 0.015},
    }


def selfcheck():
    cfg = _cfg()
    nan = np.nan

    # --- age band ---------------------------------------------------------
    a = age_fit(np.array([1900.0, 1945, 1960, 1975, 2010, nan]), cfg)
    assert a[1] == a[2] == a[3] == 1.0, a
    assert a[0] == 0.0 and a[4] == 0.0, a
    assert np.isnan(a[5])
    mid = age_fit(np.array([1927.5, 1992.5]), cfg)
    assert abs(mid[0] - 0.5) < 1e-9 and abs(mid[1] - 0.5) < 1e-9, mid
    older = dict(cfg, age=dict(cfg["age"], mode="older_better"))
    m = age_fit(np.array([1900.0, 2000.0]), older)
    assert m[0] > m[1]

    # --- percentiles ------------------------------------------------------
    g = np.array(["a", "a", "a", "b", "b"], dtype=object)
    v = np.array([1.0, 2.0, 3.0, nan, 5.0])
    hi = pct_rank(v, g, "hi")
    assert list(hi[:3]) == [0.0, 0.5, 1.0], hi
    assert np.isnan(hi[3]) and hi[4] == 0.5
    assert list(pct_rank(v, g, "lo")[:3]) == [1.0, 0.5, 0.0]

    idx = knn_index(np.array(["a"] * 4, dtype=object),
                    np.array([[0.0, 0], [1, 0], [2, 0], [100, 0]]), 3)
    assert idx.shape == (4, 3) and set(idx[3]) == {3, 2, 1}, idx
    nb = nbr_pct_rank(np.array([1.0, 2.0, 3.0, nan]), idx, "hi")
    assert np.isnan(nb[3]) and nb[0] < nb[2]

    # --- comp neighbourhood respects the radius ---------------------------
    xy = np.array([[0.0, 0], [10, 0], [5000, 0]])
    towns = np.array(["a"] * 3, dtype=object)
    cidx, cdist = comp_neighbors(xy, np.array([False, True, True]), towns, 2, 100.0)
    assert cidx[0, 0] == 1 and np.isfinite(cdist[0, 0])
    assert not np.isfinite(cdist[0, 1]), cdist   # the 5 km comp is out of range
    med, cnt = _masked_median(np.array([np.nan, 4.0, 99.0]), cidx, cdist)
    assert med[0] == 4.0 and cnt[0] == 1, (med, cnt)

    # --- resale value: paired-sales adjustment, never a bare $/sqft --------
    # One comparable: 4,000 sqft at $2.6M, i.e. $650/sqft average. A 2,000 sqft
    # replacement must NOT come out at 2,000 x 650 = $1.3M, because the land is
    # already inside the comparable.
    cfg["comps"]["marginal_sqft_share"] = 0.6
    # Wide band first, so the paired-sales arithmetic itself is checked.
    cfg["comps"]["max_implied_ppsf_share"] = 99.0
    cfg["comps"]["min_implied_ppsf_share"] = 0.0
    small = resale_value(np.array([2000.0]), np.array([2_600_000.0]),
                         np.array([4000.0]), np.array([650.0]), cfg)
    assert abs(small[0] - (2_600_000 - 0.6 * 650 * 2000)) < 1e-6, small
    assert small[0] > 2000 * 650                # and it beats naive $/sqft
    # With the band on, the implied $/sqft cannot exceed a multiple of the
    # comparables' own, in either direction.
    band = dict(cfg, comps=dict(cfg["comps"], max_implied_ppsf_share=1.25,
                                min_implied_ppsf_share=0.75))
    cap = resale_value(np.array([2000.0]), np.array([2_600_000.0]),
                       np.array([4000.0]), np.array([650.0]), band)
    assert abs(cap[0] - 1.25 * 650 * 2000) < 1e-6, cap
    assert cap[0] < small[0]                    # and it bites on small envelopes
    floor = resale_value(np.array([12000.0]), np.array([2_600_000.0]),
                         np.array([4000.0]), np.array([650.0]), band)
    assert abs(floor[0] - 0.75 * 650 * 12000) < 1e-6, floor
    same = resale_value(np.array([4000.0]), np.array([2_600_000.0]),
                        np.array([4000.0]), np.array([650.0]), cfg)
    assert same[0] == 2_600_000                 # no size difference, no adjustment
    bigger = resale_value(np.array([5000.0]), np.array([2_600_000.0]),
                          np.array([4000.0]), np.array([650.0]), cfg)
    assert bigger[0] > same[0]
    assert np.isnan(resale_value(np.array([4000.0]), np.array([np.nan]),
                                 np.array([4000.0]), np.array([650.0]), cfg)[0])

    # Leave-one-out error on comparables that are exactly linear in size is
    # small; on noise it is not. Both directions are worth pinning.
    s = np.array([3000.0, 3500, 4000, 4500, 5000, 5500])
    exact = 1_000_000 + 390 * s
    err = loo_mape(exact, s, cfg)
    assert err < 0.05, err
    assert np.isnan(loo_mape(s[:2], s[:2], cfg))

    trend = fit_price_trend(np.array([2016.0, 2018, 2020, 2022, 2024, 2019]),
                            np.array([300.0, 340, 390, 450, 520, 410]), cfg)
    assert 0.02 < trend <= 0.12, trend
    assert fit_price_trend(np.array([2020.0, 2021]), np.array([1.0, 2.0]), cfg) == 0.0
    # A handful of $1 family transfers must not be able to invert the trend.
    yr = np.array([2016.0, 2018, 2020, 2022, 2024, 2019, 2025, 2025, 2025])
    pp = np.array([300.0, 340, 390, 450, 520, 410, 0.3, 0.4, 0.2])
    assert fit_price_trend(yr, pp, cfg) < 0, "nominal deeds do skew the fit"
    assert fit_price_trend(yr[:6], pp[:6], cfg) > 0, "so the caller must filter"

    # --- envelope ---------------------------------------------------------
    d = {"lot_acres": np.array([0.25, 0.25, 0.25, 0.02]),
         "max_far": np.array([0.4, nan, nan, nan]),
         "max_lot_coverage": np.array([nan, 0.15, nan, nan])}
    raw, sized, src = buildable_envelope(
        d, cfg, observed_far=np.array([0.5, 0.5, 0.5, 0.5]),
        market_ceiling=np.array([6000.0] * 4))
    assert abs(raw[0] - 0.4 * 0.25 * SQ_FT_PER_ACRE) < 1e-6
    assert src[0] == AUTHORITATIVE and src[1] == AUTHORITATIVE
    assert src[2] == ESTIMATED and abs(raw[2] - 0.5 * 0.25 * SQ_FT_PER_ACRE) < 1e-6
    assert raw[3] < cfg["buildable"]["min_viable_sqft"]      # 0.02 ac cannot host a house
    ceiling = buildable_envelope(d, cfg, np.array([0.5] * 4),
                                 np.array([3000.0] * 4))[1]
    assert ceiling[2] == 3000.0, ceiling      # market ceiling binds

    # --- feasibility: a hard blocker is a zero, an unknown is not a penalty
    n = 4
    base = {
        "existing_living_sqft": np.array([2000.0] * n),
        "flag_wetland_m": np.array([nan, 0.0, 20.0, nan]),
        "lot_acres": np.array([0.5] * n),
        "min_lot_acres": np.array([nan, nan, nan, 1.0]),
        "flag_sewer": np.array([1.0] * n),
        "flag_flood_sfha": np.zeros(n, bool),
        "flag_acec_present": np.zeros(n, bool),
        "flag_historic_inventory": np.zeros(n, bool),
        "flag_historic_district": np.zeros(n, bool),
        "flag_historic_local_district": np.zeros(n, bool),
        "flag_historic_restriction": np.zeros(n, bool),
        "lot_compactness": np.array([0.8] * n),
        "lot_width_ft": np.array([120.0] * n),
        "min_frontage_ft": np.array([np.nan] * n),
        "n_records_on_parcel": np.ones(n),
    }
    env = np.array([4000.0] * n)
    fs, notes, masks = feasibility(base, cfg, env, env)
    assert fs[0] == 1.0 and notes[0] == ""          # unknown zoning: no penalty
    assert fs[1] == cfg["feasibility"]["wetland_overlap"], fs
    assert "wetland_overlap" in notes[1]
    assert fs[2] == cfg["feasibility"]["wetland_buffer"], fs
    # lot 0.5 ac against a 1.0 ac minimum is below 0.6x -> severely undersized
    # An authoritative frontage minimum overrides the configured fallback: a
    # 120 ft lot is wide enough by default and narrow in a 150 ft district.
    wide = feasibility(base, cfg, env, env)[2]["narrow_lot"]
    assert not wide.any(), wide
    strict = dict(base, min_frontage_ft=np.array([150.0] * n))
    assert feasibility(strict, cfg, env, env)[2]["narrow_lot"].all()

    assert masks["severely_undersized_lot"][3], masks
    assert fs[3] == cfg["feasibility"]["severely_undersized_lot"], fs
    assert not masks["nonconforming_lot"][3]    # the two bands are exclusive

    hard = dict(base, existing_living_sqft=np.array([2000.0] * n))
    fz = feasibility(hard, cfg, np.array([1000.0] * n), env)[0]
    assert (fz == 0.0).all(), fz                    # envelope below min viable

    # --- pro forma and economics -----------------------------------------
    d2 = {"existing_sqft": np.array([2400.0, 2400.0]),
          "year_built": np.array([1955.0, 1990.0]),
          "flag_sewer": np.array([1.0, 0.0])}
    pf = pro_forma(d2, cfg, arv=np.array([3_600_000.0, 3_600_000.0]),
                   buildable_sqft=np.array([4000.0, 4000.0]),
                   acq=np.array([1_000_000.0, 1_000_000.0]))
    assert pf["demo_cost"][0] > pf["demo_cost"][1]   # pre-1978 abatement
    assert pf["hard_cost"][1] > pf["hard_cost"][0]   # septic
    assert pf["profit"][0] > pf["profit"][1]
    assert 0 < pf["margin"][0] < 1, pf["margin"]
    # A cheap structure on expensive land does NOT pencil if the ARV cannot
    # carry the cost. This is the failure mode of the old single score: the
    # parcel below has a worthless structure and a 95% land share, and it loses
    # money, so it must not rank.
    poor = pro_forma(d2, cfg, arv=np.array([2_000_000.0] * 2),
                     buildable_sqft=np.array([4000.0] * 2),
                     acq=np.array([1_900_000.0] * 2))
    assert poor["margin"][0] < 0, poor["margin"]
    es = economics_score(np.array([0.25, 0.11, -0.05, nan]),
                         np.array([0.20, 0.02, -0.20, nan]),
                         np.array([nan, nan, nan, nan]), cfg)
    assert es[0] == 1.0 and 0 < es[1] < 1 and es[2] == 0.0 and np.isnan(es[3]), es

    # With the tail configured, unprofitable parcels stay ORDERED but stay far
    # below anything that pencils, so the ranking does not collapse into ties.
    tailed = dict(cfg, economics=dict(cfg["economics"],
                                      unprofitable_ceiling=0.05,
                                      tail_margin_span=0.15))
    t = economics_score(np.array([0.30, 0.05, 0.00, -0.20, -1.50, nan]),
                        np.array([0.30, 0.05, 0.00, -0.20, -1.50, nan]),
                        np.full(6, nan), tailed)
    assert t[0] == 1.0 and 0 < t[1] < 1
    assert t[2] <= 0.05 and t[2] > t[3] > t[4] > 0, t   # ordered, never negative
    assert t[3] < t[1] / 5, t             # and an order of magnitude below
    assert np.isnan(t[5])
    split = economics_score(np.array([0.11]), np.array([0.11]),
                            np.array([3.0]), cfg)
    assert split[0] > economics_score(np.array([0.11]), np.array([0.11]),
                                      np.array([1.0]), cfg)[0]

    sens = sensitivity(d2, cfg, np.array([3_000_000.0] * 2),
                       np.array([4000.0] * 2), np.array([1_200_000.0] * 2))
    assert sens["margin_if_all"][0] < sens["margin_if_acq"][0] < pf["margin"][0]

    # The implied rate must reproduce the margin it was solved for.
    rate = implied_construction_cost(d2, cfg, np.array([3_600_000.0] * 2),
                                     np.array([4000.0] * 2),
                                     np.array([1_000_000.0] * 2), 0.0)
    back = pro_forma(d2, {**cfg, "costs": {**cfg["costs"],
                                           "construction_per_sqft": rate[0]}},
                     np.array([3_600_000.0] * 2), np.array([4000.0] * 2),
                     np.array([1_000_000.0] * 2))
    assert abs(back["margin"][0]) < 1e-9, (rate, back["margin"])
    # Solving for a 20% margin must demand a cheaper build than breaking even.
    at20 = implied_construction_cost(d2, cfg, np.array([3_600_000.0] * 2),
                                     np.array([4000.0] * 2),
                                     np.array([1_000_000.0] * 2), 0.20)
    assert at20[0] < rate[0], (at20, rate)

    # --- sub-scores: weight redistributes inside a sub-score only ---------
    parts = {"a": np.array([1.0, 1.0]), "b": np.array([0.0, nan])}
    sc, cov = weighted_subscore(parts, {"a": 0.5, "b": 0.5})
    assert sc[0] == 0.5 and sc[1] == 1.0
    assert cov[0] == 1.0 and cov[1] == 0.5      # and the thin one is recorded

    prov = {k: np.array([AUTHORITATIVE, ESTIMATED, MISSING])
            for k in CONFIDENCE_INPUTS}
    conf = confidence_score(prov, cfg)
    assert conf[0] == 1.0
    assert abs(conf[1] - cfg["confidence"]["estimated_credit"]) < 1e-9
    assert conf[2] == cfg["confidence"]["floor"]

    # --- combine: a feasibility zero cannot be outvoted ------------------
    z = combine(np.array([0.0]), np.array([1.0]), np.array([1.0]),
                np.array([1.0]), np.array([1.0]), cfg)
    assert z[0] == 0.0, z
    full = combine(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                   np.array([1.0]), np.array([1.0]), cfg)
    assert abs(full[0] - 1.0) < 1e-12, full
    bare = combine(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                   np.array([0.0]), np.array([0.0]), cfg)
    assert abs(bare[0] - 1 / (1 + cfg["score"]["support_strength"])) < 1e-12
    # Support can never promote a parcel above one that also clears the core.
    weak_core = combine(np.array([1.0]), np.array([0.5]), np.array([1.0]),
                        np.array([1.0]), np.array([1.0]), cfg)
    strong_core = combine(np.array([1.0]), np.array([1.0]), np.array([1.0]),
                          np.array([0.0]), np.array([0.0]), cfg)
    assert strong_core[0] > weak_core[0], (strong_core, weak_core)
    # Thin data discounts through confidence, monotonically.
    thin = combine(np.array([1.0]), np.array([1.0]), np.array([0.25]),
                   np.array([1.0]), np.array([1.0]), cfg)
    assert thin[0] == 0.5, thin
    print("model selfcheck ok")


if __name__ == "__main__":
    selfcheck()

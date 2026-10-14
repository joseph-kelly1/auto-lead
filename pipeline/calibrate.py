#!/usr/bin/env python3
"""Does the score rank parcels that became finished, sold rebuilds?

The question this file answers is deliberately narrower than the one the old
version answered. "Was it demolished" is not the target: a demolition with no
replacement house is a failure, not a success, and is counted as a NEGATIVE.

Labels are built by comparing an older L3 vintage against the current release
for the same parcel. The statewide aggregate zip is an archive, not a snapshot:
it carries every vintage a town has ever delivered (Newton has ten, CY13 through
CY24), and it is range-readable, so labels cost a few MB rather than 7.8 GB.

For each prior vintage the model is re-scored on THAT vintage's assessor data and
asked whether the parcels that were subsequently rebuilt ranked highly. Nothing
about the outcome enters the ranking.

Label sources (--labels, comma separated):
  rebuild      YEAR_BUILT advanced >= 15 years AND the structure itself changed
               (living area moved >= 10% or building value at least doubled).
               The area/value confirmation matters: an assessor tidying up a
               wrong YEAR_BUILT is not a rebuild.
  rebuild_sold rebuild, AND the finished house then sold at a new-construction
               price. This is the default target: a completed, monetised project.
  viable       rebuild_sold, AND the recorded sale price cleared the modelled
               total project cost. The strictest label, and the only one that is
               evidence about profit rather than about activity.
  permits      permits.csv. A demolition permit followed by a new-dwelling
               permit is a success; a demolition permit with no rebuild is an
               explicit negative, reported separately.
  value_jump   BLDG_VAL grew several times faster than the town median. Reported
               for reference and NOT counted as success: this is mostly large
               renovations, which is the wrong event.

Time ordering: vintages are split by date. The older ones are the training
split, the newest prior vintage is the validation split, and any weight search
runs on training only. Outcomes are always read from the current release, which
is later than every vintage being scored.

Usage:
  uv run calibrate.py
  uv run calibrate.py --pairs 5 --target viable --fit
"""

import argparse
import itertools
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import numpy as np

import build
import model
from build import log
from httpzip import HttpFile

AGGREGATE = (f"{build.BUCKET}/{build.PREFIX}L3_AGGREGATE_SHP_20260702.zip")
KS = (50, 100, 250, 500, 1000)
# Below this a precision@K is one or two parcels wide and means nothing.
MIN_LABELS = 30
# And below this, fitting anything at all is overfitting.
MIN_LABELS_TO_FIT = 150

SUCCESS_LABELS = ("rebuild", "rebuild_sold", "viable", "permits")
REFERENCE_LABELS = ("value_jump", "demo_only")


# --------------------------------------------------------------------------
# historical vintages
# --------------------------------------------------------------------------

def aggregate_index(before_fy: dict[str, int]) -> dict[str, list[str]]:
    """town -> [Assess .dbf member paths strictly older than the live release].

    The newest vintage in the archive IS the current release, so it has to be
    excluded or "prior" data is the same data and every label disappears.
    """
    with zipfile.ZipFile(HttpFile(AGGREGATE)) as z:
        names = [n for n in z.namelist()
                 if n.endswith(".dbf") and "Assess" in Path(n).name]
    out: dict[str, list[str]] = {}
    for n in names:
        m = re.match(r"L3_SHP_M\d+_([A-Za-z]+)_CY(\d+)_FY(\d+)/", n)
        if not m:
            continue
        town, fy = m.group(1).upper(), 2000 + int(m.group(3))
        if fy < before_fy.get(town, 9999):
            out.setdefault(town, []).append(n)
    for t in out:
        out[t].sort(key=lambda n: tuple(
            int(x) for x in re.search(r"CY(\d+)_FY(\d+)", n).groups()))
    return out


def extract_vintage(member: str, raw: Path) -> Path:
    """Pull one Assess .dbf out of the remote aggregate, cached on disk."""
    dest = raw / "hist" / Path(member).name
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(HttpFile(AGGREGATE)) as z:
        data = z.read(member)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(dest)
    log(f"  extracted {Path(member).name} ({len(data) / 1e6:.1f} MB)")
    return dest


# --------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------

def label_table(con, prior_parquet: Path, current_parquet: Path, sources, cfg):
    """One row per parcel present in both vintages, with every label column.

    Success means a finished replacement house, so every success label requires
    evidence that a new structure exists - not merely that the old one left.
    """
    permits = None
    for cand in ("permits.csv", "demolition_permits.csv"):
        if (cfg["_dir"] / cand).exists():
            permits = cfg["_dir"] / cand
            break
    if "permits" in sources and permits is None:
        log("  no permits.csv / demolition_permits.csv; skipping the permit "
            "label source. This is the label the model most wants: a demolition "
            "permit joined to a subsequent new-dwelling permit is the only "
            "direct observation of the target event.")
        sources = [s for s in sources if s != "permits"]

    c = cfg["comps"]
    con.execute(f"""
    CREATE OR REPLACE TABLE eval AS
    WITH p AS (SELECT * FROM read_parquet('{prior_parquet}')),
         cur AS (SELECT loc_id, town, fy, year_built, bldg_val, land_val,
                        total_val, res_area, bld_area, site_addr,
                        ls_date, ls_price
                 FROM read_parquet('{current_parquet}')),
         j AS (
           SELECT p.town, p.loc_id, p.site_addr,
                  p.score, p.rebuild_opportunity_score,
                  p.rebuild_feasibility_score, p.project_economics_score,
                  p.acquisition_likelihood_score,
                  p.structure_obsolescence_score, p.data_confidence_score,
                  p.acq_price, p.est_acq_price, p.margin, p.arv,
                  p.buildable_sqft, p.flag_sewer,
                  p.pct_land_ratio, p.pct_impr_psf, p.pct_age_fit, p.pct_far,
                  p.pct_area_vs_nbhd, p.pct_years_since_sale,
                  p.absentee, p.estate, p.care_of,
                  p.fy AS fy_prior, cur.fy AS fy_now,
                  p.year_built AS yb_prior, cur.year_built AS yb_now,
                  p.bldg_val AS bv_prior, cur.bldg_val AS bv_now,
                  p.res_area AS ra_prior, cur.res_area AS ra_now,
                  p.existing_sqft AS gross_prior, cur.total_val AS tv_now,
                  cur.ls_date AS sale_now, cur.ls_price AS sale_price_now
           FROM p JOIN cur USING (loc_id)
           WHERE cur.town = p.town),
         infl AS (
           SELECT town, median(bv_now / nullif(bv_prior, 0)) AS med_bldg_growth
           FROM j GROUP BY town)
    SELECT j.*, i.med_bldg_growth,
           -- Already new at prior time: not a candidate, so not a fair label.
           (j.yb_prior >= j.fy_prior - 5) AS was_already_new,
           -- A new structure: the year moved AND the structure itself changed.
           -- The second half is what separates a rebuild from an assessor
           -- correcting a wrong YEAR_BUILT.
           ((j.yb_now - j.yb_prior) >= 15
            AND (j.bv_prior > 0 AND j.bv_now >= 2 * j.bv_prior
                 OR j.ra_prior > 0 AND j.ra_now > 0
                    AND abs(j.ra_now - j.ra_prior) >= 0.10 * j.ra_prior))
             AS lbl_rebuild,
           -- Reference only. Mostly large renovations, which is the wrong event,
           -- so it is never unioned into success.
           (j.bv_prior > 0
            AND j.bv_now / j.bv_prior >= 3.0 * i.med_bldg_growth) AS lbl_value_jump,
           -- The FINISHED house sold, not the lot. LS_PRICE holds only the last
           -- recorded sale, and for a spec build that is usually the completed
           -- home - but not always: one example property's last sale is the builder's
           -- $880k land purchase in the same year the new house was finished.
           -- Two tests separate them. The sale must be dated at or after the
           -- build year, and it must be priced like a finished house: at these
           -- towns' assess-to-sale ratio of ~1.0 a genuine resale lands near the
           -- new total assessment, while a teardown lot goes for a fraction.
           (j.sale_price_now >= {c["min_price"]}
            AND j.sale_now IS NOT NULL
            AND year(j.sale_now) >= j.yb_now
            AND j.tv_now > 0
            AND j.sale_price_now >= 0.85 * j.tv_now) AS sold_as_new
    FROM j JOIN infl i USING (town)
    """)

    con.execute("ALTER TABLE eval ADD COLUMN lbl_rebuild_sold BOOLEAN")
    con.execute("UPDATE eval SET lbl_rebuild_sold = lbl_rebuild AND sold_as_new")

    # `viable`: did the recorded sale of the finished house clear the modelled
    # cost of building it? Costs come from config; the sale price and the built
    # area are observed. This is the only label that is evidence about profit.
    rows = con.execute("""
      SELECT rowid AS rid, gross_prior, ra_prior, ra_now, yb_prior, flag_sewer,
             coalesce(acq_price, est_acq_price) AS acq, sale_price_now
      FROM eval""").fetchnumpy()
    f = lambda k: np.ma.filled(np.ma.asarray(rows[k]).astype("float64"), np.nan)
    d = {"existing_sqft": np.where(np.isfinite(f("gross_prior")),
                                   f("gross_prior"), f("ra_prior")),
         "year_built": f("yb_prior"), "flag_sewer": f("flag_sewer")}
    pf = model.pro_forma(d, cfg, arv=f("sale_price_now"),
                         buildable_sqft=f("ra_now"), acq=f("acq"))
    with np.errstate(invalid="ignore"):
        viable = np.isfinite(pf["profit"]) & (pf["profit"] > 0)
    con.register("viab", {"rid": np.asarray(rows["rid"], dtype="int64"),
                          "ok": viable,
                          "realised_margin": pf["margin"]})
    con.execute("""
      ALTER TABLE eval ADD COLUMN lbl_viable BOOLEAN;
      ALTER TABLE eval ADD COLUMN realised_margin DOUBLE;
    """)
    con.execute("""
      UPDATE eval SET lbl_viable = eval.lbl_rebuild_sold AND v.ok,
                      realised_margin = v.realised_margin
      FROM viab v WHERE v.rid = eval.rowid
    """)

    if "permits" in sources:
        con.execute(f"""
        CREATE OR REPLACE TEMP VIEW perm AS
          SELECT * FROM read_csv_auto('{permits}');
        """)
        cols = {r[0].lower() for r in con.execute("DESCRIBE perm").fetchall()}
        kind = ("lower(perm.permit_type)" if "permit_type" in cols
                else "'demolition'")
        match = ("(perm.loc_id = eval.loc_id OR upper(perm.site_addr) = "
                 "upper(eval.site_addr)) AND upper(perm.town) = upper(eval.town)")
        con.execute(f"""
        ALTER TABLE eval ADD COLUMN lbl_permits BOOLEAN;
        ALTER TABLE eval ADD COLUMN lbl_demo_only BOOLEAN;
        -- A demolition permit is only a success when a new dwelling followed it.
        UPDATE eval SET lbl_permits = EXISTS (
          SELECT 1 FROM perm WHERE {match}
            AND {kind} LIKE 'demol%')
          AND (EXISTS (SELECT 1 FROM perm WHERE {match}
                       AND {kind} LIKE 'new%')
               OR eval.lbl_rebuild);
        -- Demolished with nothing built back. Explicitly NOT a success.
        UPDATE eval SET lbl_demo_only = EXISTS (
          SELECT 1 FROM perm WHERE {match} AND {kind} LIKE 'demol%')
          AND NOT coalesce(eval.lbl_permits, false) AND NOT eval.lbl_rebuild;
        """)
    return sources


def set_target(con, target: str):
    """`success` is one named label, never a union of everything available."""
    con.execute("ALTER TABLE eval ADD COLUMN IF NOT EXISTS success BOOLEAN")
    con.execute(f"UPDATE eval SET success = coalesce(lbl_{target}, false) "
                f"AND NOT was_already_new")
    return con.execute("SELECT count(*) FROM eval WHERE success").fetchone()[0]


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def rank_metrics(score: np.ndarray, y: np.ndarray) -> dict:
    """precision@K, lift, recall, AUC and a decile calibration table."""
    ok = np.isfinite(score)
    score, y = score[ok], y[ok]
    order = np.argsort(-score, kind="stable")
    y = y[order]
    s = score[order]
    n, pos = len(y), int(y.sum())
    base = pos / n if n else 0.0
    res = {"n": n, "labels": pos, "base_rate": base}
    res["p_at_k"] = [(k, float(y[:k].sum()) / k,
                      float(y[:k].sum()) / pos if pos else 0.0)
                     for k in KS if k <= n]
    if 0 < pos < n:
        ranks = np.arange(n)
        res["auc"] = 1 - (ranks[y].mean() - (pos - 1) / 2) / (n - pos)
    else:
        res["auc"] = float("nan")
    res["score_pos"] = float(np.median(s[y])) if pos else float("nan")
    res["score_neg"] = float(np.median(s[~y])) if (~y).any() else float("nan")
    # The score is not a probability, so this is a monotonicity check rather
    # than a Brier-style calibration: does a higher decile really contain more
    # successes than the one below it?
    dec = np.array_split(np.arange(n), 10)
    res["deciles"] = [(i + 1, float(s[g].mean()), float(y[g].mean()), len(g))
                      for i, g in enumerate(dec) if len(g)]
    rates = [r for _, _, r, _ in res["deciles"]]
    res["monotone"] = all(a >= b - 1e-12 for a, b in zip(rates, rates[1:]))
    return res


def report(title, r, extra=""):
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")
    print(f"eligible {r['n']:,}   successes {r['labels']:,}"
          f"   base rate {r['base_rate']:.2%}{extra}")
    if r["labels"] < MIN_LABELS:
        print(f"  ** {r['labels']} labels is below {MIN_LABELS}. Every number "
              f"below is directional only; this is not validation. **")
    print(f"  AUC {r['auc']:.3f}   median score: successes {r['score_pos']:.3f}"
          f" vs rest {r['score_neg']:.3f}")
    print(f"\n  {'K':>6} {'precision':>10} {'lift':>7} {'recall':>8}")
    for k, p, rec in r["p_at_k"]:
        lift = p / r["base_rate"] if r["base_rate"] else float("nan")
        print(f"  {k:>6} {p:>10.2%} {lift:>6.1f}x {rec:>8.1%}")
    print(f"\n  calibration by score decile "
          f"({'monotone' if r['monotone'] else 'NOT monotone'})")
    print(f"  {'decile':>7} {'mean score':>11} {'success rate':>13} {'n':>7}")
    for i, ms, rate, cnt in r["deciles"]:
        print(f"  {i:>7} {ms:>11.3f} {rate:>13.2%} {cnt:>7,}")


def compare(con, columns: dict[str, str], y: np.ndarray, arrays: dict):
    """Before/after on the same labels and the same parcels."""
    print(f"\n  {'ranking by':<32}{'AUC':>7}{'P@50':>8}{'P@100':>8}"
          f"{'P@250':>8}{'recall@250':>12}")
    out = {}
    for label, col in columns.items():
        r = rank_metrics(arrays[col], y)
        p = dict((k, (pr, rec)) for k, pr, rec in r["p_at_k"])
        cell = lambda k, i: (f"{p[k][i]:.1%}" if k in p else "-")
        print(f"  {label:<32}{r['auc']:>7.3f}{cell(50, 0):>8}"
              f"{cell(100, 0):>8}{cell(250, 0):>8}{cell(250, 1):>12}")
        out[label] = r
    return out


def precision_at(score: np.ndarray, y: np.ndarray, ks) -> dict[int, float]:
    """precision@K for arbitrary K, on one ranking. NaN scores rank last."""
    ok = np.isfinite(score)
    ordered = y[ok][np.argsort(-score[ok], kind="stable")]
    return {k: float(ordered[:k].sum()) / k for k in ks if k <= len(ordered)}


def per_town(arrays, y, towns, target):
    """Reported separately by town: a statewide precision hides which market
    the model actually works in."""
    print(f"\n  by town (ranking within the town, on {target} labels)")
    print(f"  {'town':<12}{'n':>8}{'successes':>11}{'base':>8}{'AUC':>7}"
          f"{'P@25':>8}{'P@50':>8}{'P@100':>8}")
    for t in sorted(set(towns)):
        m = towns == t
        score = arrays["rebuild_opportunity_score"][m]
        r = rank_metrics(score, y[m])
        p = precision_at(score, y[m], (25, 50, 100))
        cell = lambda k: (f"{p[k]:.1%}" if k in p else "-")
        print(f"  {t:<12}{r['n']:>8,}{r['labels']:>11,}{r['base_rate']:>8.2%}"
              f"{r['auc']:>7.3f}{cell(25):>8}{cell(50):>8}{cell(100):>8}")


def cost_check(con, cfg, target):
    """Check `costs.construction_per_sqft` against projects that really happened.

    For every parcel that was rebuilt and sold, the resale price and the built
    floor area are observed. Solving the pro forma for the construction rate that
    would have made each of those projects break even, and separately hit the
    configured target margin, says what the assumption should be. Builders who
    completed these projects presumably did not lose money on most of them, so a
    configured rate above the break-even distribution is the assumption being
    wrong rather than the market being unbuildable.
    """
    rows = con.execute(f"""
      SELECT town, gross_prior, ra_prior, ra_now, yb_prior, flag_sewer,
             coalesce(acq_price, est_acq_price) AS acq, sale_price_now
      FROM eval
      WHERE success AND ra_now > 0 AND sale_price_now > 0
        AND coalesce(acq_price, est_acq_price) > 0""").fetchnumpy()
    n = len(rows["town"])
    if not n:
        return
    f = lambda k: np.ma.filled(np.ma.asarray(rows[k]).astype("float64"), np.nan)
    d = {"existing_sqft": np.where(np.isfinite(f("gross_prior")),
                                   f("gross_prior"), f("ra_prior")),
         "year_built": f("yb_prior"), "flag_sewer": f("flag_sewer")}
    arv, sqft, acq = f("sale_price_now"), f("ra_now"), f("acq")
    tgt = cfg["economics"]["target_margin"]
    breakeven = model.implied_construction_cost(d, cfg, arv, sqft, acq, 0.0)
    at_target = model.implied_construction_cost(d, cfg, arv, sqft, acq, tgt)
    configured = cfg["costs"]["construction_per_sqft"]

    print(f"\n  cost assumption checked against the {n} completed-and-sold "
          f"rebuilds in this window")
    print(f"  {'town':<12}{'n':>5}{'break-even $/sqft (p25/med/p75)':>34}"
          f"{f'at {tgt:.0%} margin (med)':>24}")
    towns = np.asarray(rows["town"], dtype=object)
    for t in sorted(set(towns)) + ["ALL"]:
        m = np.ones(n, bool) if t == "ALL" else (towns == t)
        b = breakeven[m & np.isfinite(breakeven)]
        a = at_target[m & np.isfinite(at_target)]
        if len(b) < 5:
            continue
        q = np.percentile(b, [25, 50, 75])
        print(f"  {t:<12}{len(b):>5}"
              f"{f'${q[0]:,.0f} / ${q[1]:,.0f} / ${q[2]:,.0f}':>34}"
              f"{f'${np.median(a):,.0f}':>24}")
    share = float(np.mean(breakeven[np.isfinite(breakeven)] >= configured))
    print(f"  configured ${configured:,.0f}/sqft would have broken even on "
          f"{share:.0%} of them.")
    if share < 0.5:
        print(f"  -> Set costs.construction_per_sqft from your builder's actual "
              f"numbers. Above the median break-even, the model calls most real "
              f"completed rebuilds unprofitable, which collapses "
              f"project_economics_score and with it the whole ranking.")


# --------------------------------------------------------------------------
# weight search
# --------------------------------------------------------------------------

# Only the combination weights are searchable this way: the five sub-scores are
# already in the parquet, so recombining them is arithmetic rather than a
# rebuild. Feasibility multipliers and cost assumptions are NOT searched - they
# are dollar and regulatory statements, and there are no labels that identify
# which gate blocked which deal.
GRID = {
    "confidence_exponent": [0.0, 0.25, 0.5, 1.0],
    # 0.0 means "ignore the pro forma entirely". It is in the grid because the
    # comparison is the whole point: a resale-value model with 18-33%
    # out-of-sample error is a noisy multiplier, and multiplying by noise can
    # destroy more ordering than the economics adds.
    # Deliberately reaching well past the configured 0.35: if the labels say
    # structure obsolescence carries most of the signal, the grid has to be able
    # to say so rather than being fenced in by the prior.
    "support_strength": [0.0, 0.35, 1.0, 2.0, 4.0, 9.0],
    "support_acquisition_likelihood": [0.2, 0.4, 0.6, 0.8],
    "economics_exponent": [0.0, 0.25, 0.5, 1.0, 1.5],
}

# Printed on every run: the two levers that decide whether the pro forma or the
# obsolescence signals drive the ranking, measured on the labels rather than
# argued about.
SWEEP = {"economics_exponent": [0.0, 0.25, 0.5, 1.0, 1.5],
         "support_strength": [0.0, 0.35, 1.0, 4.0]}


def sweep(arrays, y, cfg):
    """One-parameter-at-a-time sensitivity of the ranking to the two levers."""
    print("\n  sensitivity of the ranking to the combination levers "
          "(one at a time, everything else as configured)")
    for key, values in SWEEP.items():
        cells = []
        for v in values:
            s = recombine(arrays, {key: v}, cfg)
            r = rank_metrics(s, y)
            p = precision_at(s, y, (100,)).get(100, float("nan"))
            mark = "*" if v == cfg["score"][key] else " "
            cells.append(f"{v:g}{mark} AUC {r['auc']:.3f} P@100 {p:.1%}")
        print(f"    {key:<22}" + "   ".join(cells))
    ref = rank_metrics(arrays["structure_obsolescence_score"], y)
    refp = precision_at(arrays["structure_obsolescence_score"], y,
                        (100,)).get(100, float("nan"))
    print(f"    {'obsolescence only':<22}   AUC {ref['auc']:.3f} "
          f"P@100 {refp:.1%}   (* = configured value)")


def recombine(arrays, params, cfg):
    s = dict(cfg["score"], **params)
    s["support_structure_obsolescence"] = 1 - s["support_acquisition_likelihood"]
    return model.combine(arrays["rebuild_feasibility_score"],
                         arrays["project_economics_score"],
                         arrays["data_confidence_score"],
                         arrays["acquisition_likelihood_score"],
                         arrays["structure_obsolescence_score"],
                         {**cfg, "score": s})


def search_weights(train, valid, cfg, at_k=100):
    """Grid search on TRAIN precision@K, reported on VALIDATION.

    Refuses to run below MIN_LABELS_TO_FIT rather than dressing up a fit on
    forty labels as optimisation.
    """
    n_train = int(train["y"].sum())
    if n_train < MIN_LABELS_TO_FIT:
        print(f"\n  NOT fitting weights: the training split carries {n_train} "
              f"successes, below the {MIN_LABELS_TO_FIT} floor. The weights in "
              f"config.toml are expert settings and are reported as such; "
              f"nothing here was learned.")
        return None

    keys = list(GRID)
    best, best_p = None, -1.0
    for combo in itertools.product(*(GRID[k] for k in keys)):
        params = dict(zip(keys, combo))
        s = recombine(train["arrays"], params, cfg)
        r = rank_metrics(s, train["y"])
        p = dict((k, pr) for k, pr, _ in r["p_at_k"]).get(at_k)
        if p is not None and p > best_p:
            best, best_p = params, p
    print(f"\n  grid search over {np.prod([len(GRID[k]) for k in keys])} "
          f"combinations, selected on TRAIN precision@{at_k}")
    print(f"    train P@{at_k} {best_p:.2%}   params {best}")
    for split_name, split in (("train", train), ("validation", valid)):
        for name, params in (("config", {}), ("fitted", best)):
            s = recombine(split["arrays"], params, cfg)
            r = rank_metrics(s, split["y"])
            p = dict((k, pr) for k, pr, _ in r["p_at_k"])
            print(f"    {split_name:<11}{name:<8} AUC {r['auc']:.3f}   "
                  f"P@50 {p.get(50, float('nan')):.1%}   "
                  f"P@100 {p.get(100, float('nan')):.1%}   "
                  f"P@250 {p.get(250, float('nan')):.1%}")
    print("    Treat the fitted row as a candidate, not a conclusion: the grid "
          "was selected on one metric on one split.")
    return best


# --------------------------------------------------------------------------

RANKINGS = {
    "rebuild_opportunity_score (new)": "rebuild_opportunity_score",
    "structure_obsolescence only (old model)": "structure_obsolescence_score",
    "project_economics only": "project_economics_score",
    "rebuild_feasibility only": "rebuild_feasibility_score",
    "acquisition_likelihood only": "acquisition_likelihood_score",
}

EVAL_COLUMNS = [
    "rebuild_opportunity_score", "rebuild_feasibility_score",
    "project_economics_score", "acquisition_likelihood_score",
    "structure_obsolescence_score", "data_confidence_score",
]


def fetch_eval(con):
    cols = ", ".join(EVAL_COLUMNS)
    rows = con.execute(f"""
      SELECT town, success, realised_margin, {cols}
      FROM eval WHERE rebuild_opportunity_score IS NOT NULL""").fetchnumpy()
    arrays = {c: np.ma.filled(np.ma.asarray(rows[c]).astype("float64"), np.nan)
              for c in EVAL_COLUMNS}
    return (arrays, np.asarray(rows["success"], dtype=bool),
            np.asarray(rows["town"], dtype=object),
            np.ma.filled(np.ma.asarray(rows["realised_margin"]).astype("float64"),
                         np.nan))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--pairs", type=int, default=3,
                    help="how many prior vintages to evaluate, newest first")
    ap.add_argument("--labels", default="rebuild,rebuild_sold,viable,permits")
    ap.add_argument("--target", default="rebuild_sold",
                    choices=SUCCESS_LABELS,
                    help="which label counts as success for the metrics")
    ap.add_argument("--fit", action="store_true",
                    help="attempt a grid search over the combination weights")
    args = ap.parse_args()

    cfg = build.load_config(args.config)
    out = cfg["_out"]
    sources = [s.strip() for s in args.labels.split(",") if s.strip()]

    current = out / "parcels.parquet"
    if not current.exists():
        raise SystemExit("run build.py first; parcels.parquet is missing")

    live_fy = dict(duckdb.connect().execute(
        f"SELECT upper(town), max(fy) FROM read_parquet('{current}') GROUP BY 1"
    ).fetchall())
    log(f"live release fiscal years: {live_fy}")
    log("indexing the statewide aggregate archive")
    index = aggregate_index(live_fy)
    towns = cfg["run"]["towns"]
    missing = [t for t in towns if t.upper() not in index]
    if missing:
        log(f"  no historical vintages for {missing}")

    depth = min(len(index[t.upper()]) for t in towns if t.upper() in index)
    n_back = min(args.pairs, depth)
    splits = {}

    for back in range(1, n_back + 1):
        override, tag = {}, []
        for t in towns:
            if t.upper() not in index:
                continue
            member = index[t.upper()][-back]
            override[t] = extract_vintage(member, cfg["_raw"])
            tag.append(re.search(r"CY\d+_FY\d+", member).group(0))
        name = f"prior_{back}"
        con, prior_parquet = build.run(cfg, assess_override=override,
                                       name=name, tiles=False, quiet=True)
        used = label_table(con, prior_parquet, current, sources, cfg)
        n_success = set_target(con, args.target)
        arrays, y, town_col, realised = fetch_eval(con)

        span = con.execute(
            "SELECT round(avg(fy_now - fy_prior), 1) FROM eval").fetchone()[0]
        counts = {s: con.execute(
            f"SELECT count(*) FROM eval WHERE coalesce(lbl_{s}, false)"
            f" AND NOT was_already_new").fetchone()[0]
            for s in list(used) + ["value_jump"]
            + (["demo_only"] if "permits" in used else [])
            if s != "permits" or "permits" in used}

        r = rank_metrics(arrays["rebuild_opportunity_score"], y)
        # The observation window dominates everything here: a candidate that has
        # not been rebuilt YET counts as a negative, so short horizons understate
        # precision badly.
        report(f"scored on {', '.join(sorted(set(tag)))}  ->  outcome as of the "
               f"current release ({span} fiscal years later)\n"
               f"target label: {args.target}", r,
               extra=f"\nlabel counts: "
                     + ", ".join(f"{k}={v}" for k, v in counts.items())
                     + "\n              value_jump is reported, never counted as "
                       "success: it is mostly renovation")
        compare(con, RANKINGS, y, arrays)
        sweep(arrays, y, cfg)
        per_town(arrays, y, town_col, args.target)

        with np.errstate(invalid="ignore"):
            got = realised[y & np.isfinite(realised)]
        if len(got):
            print(f"\n  realised margin of the {len(got)} successes, using the "
                  f"observed resale price and configured costs: "
                  f"median {np.median(got):.1%}, "
                  f"{int((got > 0).sum())} of {len(got)} profitable")
        cost_check(con, cfg, args.target)

        splits[back] = {"arrays": arrays, "y": y, "tag": sorted(set(tag))}
        con.close()
        prior_parquet.unlink(missing_ok=True)
        (out / f"{name}_completeness.csv").unlink(missing_ok=True)

    if args.fit:
        if n_back < 2:
            print("\n  --fit needs at least two vintages so that training and "
                  "validation can be separated in time. Re-run with --pairs 2+.")
            return
        # Oldest vintages train; the most recent prior vintage validates. Never
        # the other way round: the newest split must not inform the weights.
        train_keys = sorted(splits)[1:]
        train = {"arrays": {c: np.concatenate([splits[k]["arrays"][c]
                                               for k in train_keys])
                            for c in EVAL_COLUMNS},
                 "y": np.concatenate([splits[k]["y"] for k in train_keys])}
        valid = splits[sorted(splits)[0]]
        print(f"\n{'=' * 74}\ntime-based split: train on "
              f"{[t for k in train_keys for t in splits[k]['tag']]}, "
              f"validate on {valid['tag']}\n{'=' * 74}")
        search_weights(train, valid, cfg)


if __name__ == "__main__":
    main()

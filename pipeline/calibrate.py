#!/usr/bin/env python3
"""Does the score actually rank historical teardowns highly?

Labels come from comparing an older L3 vintage against the current release for
the same parcel. The statewide aggregate zip is an archive, not a snapshot: it
carries every vintage a town has ever delivered (Newton has ten, CY13 through
CY24), and it is range-readable, so labels cost a few MB rather than 7.8 GB.

For each prior vintage the model is re-scored on THAT vintage's assessor data
and asked whether the parcels that were subsequently torn down were ranked
highly. Nothing about the outcome leaks into the ranking.

Label sources (--labels, comma separated):
  year_built  a parcel's YEAR_BUILT advanced by >= 15 years. Near ground truth:
              the assessor only moves it when the structure is replaced.
  value_jump  BLDG_VAL grew several times faster than the town's median growth
              over the same interval, which controls for revaluation inflation.
  permits     demolition_permits.csv, if you have one. Columns: town, loc_id
              (or site_addr), permit_date. Preferred when available.

Usage:
  uv run calibrate.py                       # default: 3 most recent vintages
  uv run calibrate.py --pairs 5 --labels year_built
"""

import argparse
import re
import sys
import zipfile
from pathlib import Path

import duckdb
import numpy as np

import build
from build import COMPONENTS, log
from httpzip import HttpFile

AGGREGATE = (f"{build.BUCKET}/{build.PREFIX}L3_AGGREGATE_SHP_20260702.zip")
KS = (50, 100, 250, 500, 1000)
MIN_LABELS = 30      # below this, precision@K is directional only


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
    """One row per parcel present in both vintages, with a `torn_down` flag."""
    permits = cfg["_dir"] / "demolition_permits.csv"
    if "permits" in sources and not permits.exists():
        log(f"  no {permits.name}; skipping the permit label source")
        sources = [s for s in sources if s != "permits"]

    con.execute(f"""
    CREATE OR REPLACE TABLE eval AS
    WITH p AS (SELECT * FROM read_parquet('{prior_parquet}')),
         c AS (SELECT loc_id, town, fy, year_built, bldg_val, land_val,
                      res_area, site_addr
               FROM read_parquet('{current_parquet}')),
         j AS (
           SELECT p.town, p.loc_id, p.site_addr, p.score, p.score_coverage,
                  p.fy AS fy_prior, c.fy AS fy_now,
                  p.year_built AS yb_prior, c.year_built AS yb_now,
                  p.bldg_val AS bv_prior, c.bldg_val AS bv_now,
                  p.land_val AS lv_prior, c.land_val AS lv_now,
                  p.res_area AS ra_prior, c.res_area AS ra_now
           FROM p JOIN c USING (loc_id)
           WHERE c.town = p.town),
         infl AS (
           SELECT town,
                  median(bv_now / nullif(bv_prior, 0)) AS med_bldg_growth
           FROM j GROUP BY town)
    SELECT j.*, i.med_bldg_growth,
           -- Already new at prior time: not a candidate, so not a fair label.
           (j.yb_prior >= j.fy_prior - 5) AS was_already_new,
           (j.yb_now - j.yb_prior) >= 15  AS lbl_year_built,
           (j.bv_prior > 0
            AND j.bv_now / j.bv_prior >= 3.0 * i.med_bldg_growth) AS lbl_value_jump
    FROM j JOIN infl i USING (town)
    """)

    if "permits" in sources:
        con.execute(f"""
        CREATE OR REPLACE TEMP VIEW perm AS
          SELECT * FROM read_csv_auto('{permits}');
        ALTER TABLE eval ADD COLUMN lbl_permits BOOLEAN;
        UPDATE eval SET lbl_permits = EXISTS (
          SELECT 1 FROM perm
          WHERE (perm.loc_id = eval.loc_id
                 OR upper(perm.site_addr) = upper(eval.site_addr))
            AND upper(perm.town) = upper(eval.town));
        """)

    pred = " OR ".join(f"coalesce(lbl_{s}, false)" for s in sources)
    con.execute(f"ALTER TABLE eval ADD COLUMN torn_down BOOLEAN")
    con.execute(f"UPDATE eval SET torn_down = ({pred}) AND NOT was_already_new")
    return sources


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def evaluate(con, sources) -> dict:
    rows = con.execute("""
      SELECT town, score, torn_down, score_coverage
      FROM eval WHERE score IS NOT NULL ORDER BY score DESC
    """).fetchnumpy()
    y = np.asarray(rows["torn_down"], dtype=bool)
    n, pos = len(y), int(y.sum())
    base = pos / n if n else 0.0
    res = {"n": n, "labels": pos, "base_rate": base,
           "per_town": con.execute("""
             SELECT town, count(*) AS n, count(*) FILTER (WHERE torn_down) AS labels
             FROM eval GROUP BY town ORDER BY town""").fetchall(),
           "by_source": [(s, con.execute(
               f"SELECT count(*) FROM eval WHERE coalesce(lbl_{s}, false)"
               " AND NOT was_already_new").fetchone()[0]) for s in sources]}
    res["p_at_k"] = [(k, float(y[:k].sum()) / k, float(y[:k].sum()) / pos if pos else 0.0)
                     for k in KS if k <= n]
    # Rank-based separation: probability a random teardown outranks a random
    # non-teardown. 0.5 is worthless, 1.0 is perfect.
    if 0 < pos < n:
        ranks = np.arange(n)
        res["auc"] = 1 - (ranks[y].mean() - (pos - 1) / 2) / (n - pos)
    else:
        res["auc"] = float("nan")
    s = np.asarray(rows["score"], dtype=float)
    res["score_pos"] = (float(np.median(s[y])) if pos else float("nan"))
    res["score_neg"] = float(np.median(s[~y]))
    return res


def correlation_matrix(con, parquet: Path):
    cols = [f"pct_{c}" for c in COMPONENTS]
    d = con.execute(
        f"SELECT {', '.join(cols)} FROM read_parquet('{parquet}')").fetchnumpy()
    # DuckDB hands back masked arrays for nullable columns; asarray would keep
    # the fill values and silently pass an all-null component through.
    m = np.column_stack(
        [np.ma.filled(np.ma.asarray(d[c]).astype(float), np.nan) for c in cols])
    keep = [i for i in range(m.shape[1]) if not np.isnan(m[:, i]).all()]
    m, names = m[:, keep], [cols[i][4:] for i in keep]
    ok = ~np.isnan(m).any(axis=1)
    c = np.corrcoef(m[ok].T)
    w = max(len(n) for n in names) + 1
    print(f"\nComponent percentile correlation (n={ok.sum()}, "
          f"{m.shape[1] - len(keep)} all-null components omitted)")
    print(" " * w + "".join(f"{n[:7]:>9}" for n in names))
    for i, n in enumerate(names):
        print(f"{n:<{w}}" + "".join(
            f"{c[i, j]:>9.2f}" for j in range(len(names))))
    hot = [(names[i], names[j], c[i, j])
           for i in range(len(names)) for j in range(i + 1, len(names))
           if abs(c[i, j]) >= 0.8]
    for a, b, v in hot:
        print(f"  {a} and {b} correlate at {v:.2f}: near-duplicate signals, "
              f"consider collapsing into one weighted component")
    if not hot:
        print("  no pair above 0.8; the components are carrying distinct information")


def report(title, r, sources):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"eligible parcels {r['n']:,}   labelled teardowns {r['labels']:,}"
          f"   base rate {r['base_rate']:.2%}")
    print("  by label source: " + ", ".join(f"{s}={n}" for s, n in r["by_source"]))
    print("  by town:         " + ", ".join(
        f"{t}={lab}/{n:,}" for t, n, lab in r["per_town"]))
    if r["labels"] < MIN_LABELS:
        print(f"  ** {r['labels']} labels is below {MIN_LABELS}. Treat every "
              f"number below as directional only; this is not validation. **")
    print(f"\n  AUC {r['auc']:.3f}   "
          f"median score: teardowns {r['score_pos']:.3f} vs rest {r['score_neg']:.3f}")
    print(f"\n  {'K':>6} {'precision':>10} {'lift':>7} {'recall':>8}")
    for k, p, rec in r["p_at_k"]:
        lift = p / r["base_rate"] if r["base_rate"] else float("nan")
        print(f"  {k:>6} {p:>10.2%} {lift:>6.1f}x {rec:>8.1%}")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--pairs", type=int, default=3,
                    help="how many prior vintages to evaluate, newest first")
    ap.add_argument("--labels", default="year_built,value_jump,permits")
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

    # Deepest common history across the target towns, newest first.
    depth = min(len(index[t.upper()]) for t in towns if t.upper() in index)
    for back in range(1, min(args.pairs, depth) + 1):
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
        r = evaluate(con, used)
        # The observation window matters more than anything else here: a
        # candidate that has not been torn down yet counts as a negative, so
        # short horizons understate precision badly.
        span = con.execute(
            "SELECT round(avg(fy_now - fy_prior), 1) FROM eval").fetchone()[0]
        report(f"scored on {', '.join(sorted(set(tag)))}  ->  outcome as of the "
               f"current release ({span} fiscal years later)", r, used)
        if back == 1:
            correlation_matrix(con, current)
        con.close()
        prior_parquet.unlink(missing_ok=True)
        (out / f"{name}_completeness.csv").unlink(missing_ok=True)


if __name__ == "__main__":
    main()

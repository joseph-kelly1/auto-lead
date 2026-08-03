#!/usr/bin/env python3
"""MassGIS Level 3 parcels -> scored parcels.parquet + parcels.pmtiles.

Idempotent and resumable: downloaded town archives are cached in raw_dir and
reused. Delete a town's zip to force a re-fetch.

All areas, distances and neighbourhoods are computed in EPSG:26986
(Mass State Plane Mainland, metres). Reprojection to 4326 happens once, at the
tile-writing step.

Usage:  uv run build.py [config.toml]
"""

import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import numpy as np
from scipy.spatial import cKDTree

BUCKET = "https://s3.us-east-1.amazonaws.com/download.massgis.digital.mass.gov"
PREFIX = "shapefiles/l3parcels/"
SQ_M_PER_ACRE = 4046.8564224
SQ_FT_PER_ACRE = 43560.0

# Direction each component is ranked in. "hi" = a larger raw value is a
# stronger teardown signal and should receive a higher percentile.
# absentee is boolean and enters the score directly as 0/1, not as a percentile.
COMPONENTS = {
    "land_ratio": "hi",
    "impr_psf": "lo",
    "age_fit": "hi",
    "far": "lo",
    "area_vs_nbhd": "lo",
    "lot_vs_zoning_min": "hi",
    "years_since_sale": "hi",
}

# Which column of `comp` each component's coverage is judged on.
SOURCE_FIELD = {
    "land_ratio": "land_ratio",
    "impr_psf": "impr_psf",
    "age_fit": "year_built",
    "far": "far",
    "area_vs_nbhd": "res_area",
    "lot_vs_zoning_min": "min_lot_acres",
    "years_since_sale": "years_since_sale",
}

# Reported per town. Measured on `comp`, i.e. after 0-means-missing and the
# sentinel-date and $0-structure corrections, so the numbers are what scoring
# actually sees rather than what the DBF nominally contains.
COMPLETENESS_FIELDS = [
    "land_ratio", "impr_psf", "far", "years_since_sale", "year_built",
    "res_area", "bld_area", "lot_acres", "gis_acres", "ls_price",
    "min_lot_acres", "zoning", "own_addr",
]

# MassGIS constraint layers, all published in EPSG:26986 so no reprojection is
# needed before measuring. Each produces a "needs review" marker, never a
# conclusion: the authoritative answer is a wetlands delineation, a Title 5
# inspection or a call to the building department.
LAYER_SOURCES = {
    "wetland": ("wetlandsdep_poly", "WETLANDSDEP_POLY.shp"),
    # POTW = publicly owned treatment works, i.e. municipal sewer. The NONPOTW
    # layer in the same archive is private package plants serving one campus or
    # condo, which is not what "is this house on town sewer" means.
    "sewer": ("DEP_Sewer_Service_Areas", "SEWER_SERVICE_AREA_POTW_POLY.shp"),
    "flood": ("nfhl", "FEMA_NFHL_POLY.shp"),
    "acec": ("acecs", "acecs_poly.shp"),
}

RAW_COLUMNS = """
  town VARCHAR, town_id INTEGER, release VARCHAR, loc_id VARCHAR,
  prop_id VARCHAR, fy INTEGER, use_code VARCHAR, use_desc VARCHAR,
  site_addr VARCHAR, owner1 VARCHAR, own_addr VARCHAR, own_city VARCHAR,
  city VARCHAR, own_state VARCHAR, zoning VARCHAR, style VARCHAR,
  land_val BIGINT, bldg_val BIGINT, other_val BIGINT, total_val BIGINT,
  year_built INTEGER, res_area BIGINT, bld_area BIGINT, ls_price BIGINT,
  lot_acres DOUBLE, ls_date DATE, gis_acres DOUBLE,
  x26986 DOUBLE, y26986 DOUBLE, n_records_on_parcel BIGINT
"""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# ingest
# --------------------------------------------------------------------------

def town_keys(raw: Path) -> dict[str, str]:
    """Map TOWNNAME -> S3 key. Listed once, then cached."""
    cache = raw / "l3_keys.json"
    if cache.exists():
        return json.loads(cache.read_text())
    keys, token = {}, ""
    while True:
        url = f"{BUCKET}?list-type=2&prefix={PREFIX}&max-keys=1000"
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        body = urllib.request.urlopen(url).read().decode()
        for k in re.findall(r"<Key>([^<]+)</Key>", body):
            m = re.match(rf"{re.escape(PREFIX)}L3_SHP_M(\d+)_(.+)\.zip$", k)
            if m:
                keys[m.group(2).upper()] = k
        m = re.search(r"<NextContinuationToken>([^<]+)</", body)
        if not m:
            break
        token = m.group(1)
    cache.write_text(json.dumps(keys, indent=1, sort_keys=True))
    return keys


def fetch_town(town: str, keys: dict[str, str], raw: Path) -> Path:
    """Download + unzip one town unless already cached. Returns its shp dir."""
    key = keys.get(town.upper().replace(" ", ""))
    if not key:
        near = sorted(t for t in keys if town.upper()[:4] in t)
        raise SystemExit(f"no L3 archive for {town!r}; did you mean {near}?")
    zpath = raw / Path(key).name
    dest = raw / zpath.stem
    if not dest.exists():
        if not zpath.exists():
            log(f"  downloading {key}")
            tmp = zpath.with_suffix(".part")
            urllib.request.urlretrieve(f"{BUCKET}/{key}", tmp)
            tmp.rename(zpath)  # rename-on-complete keeps partials out of cache
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
    found = list(dest.rglob("*TaxPar*.shp"))
    if not found:
        raise SystemExit(f"no TaxPar shapefile inside {dest}")
    return found[0].parent


def load_town(con, town: str, d: Path, cfg, assess: Path | None = None) -> int:
    """Join TaxPar to Assess for one town, filter, append to raw_parcels.

    `assess` overrides the Assess table so calibrate.py can re-score a prior
    vintage against the current geometry.
    """
    taxpar = next(d.glob("*TaxPar*.shp"))
    assess = assess or next(d.glob("*Assess*.dbf"))
    uc_lut = next(iter(d.glob("*UC_LUT*.dbf")), None)
    # Release vintage lives in the filename (M288Assess_CY25_FY26.dbf) as well
    # as in the FY column; towns within one L3 release differ in both.
    release = assess.stem.split("_", 1)[1] if "_" in assess.stem else ""

    poly_types = ", ".join(f"'{p}'" for p in cfg["filters"]["poly_types"])
    include = ", ".join(f"'{u}'" for u in cfg["filters"]["include_use_codes"])
    excl_pred = " OR ".join(
        f"uc LIKE '{p}%'" for p in cfg["filters"]["exclude_parcel_if_any_use_prefix"])

    con.execute(f"""
    CREATE OR REPLACE TEMP VIEW tp AS
      SELECT LOC_ID AS loc_id, geom,
             ST_Area(geom) AS shape_area_m2,
             ST_X(ST_Centroid(geom)) AS x26986,
             ST_Y(ST_Centroid(geom)) AS y26986
      FROM ST_Read('{taxpar}')
      WHERE POLY_TYPE IN ({poly_types}) AND LOC_ID IS NOT NULL AND LOC_ID <> '';

    CREATE OR REPLACE TEMP VIEW asr AS
      -- USE_CODE is C(4). Most towns store the 3-digit DOR class plus a
      -- one-character local detail ('1010', '1013' single family waterfront,
      -- '930V', '102L'); some store the bare 3-digit class. Either way the
      -- class is the first three characters. Verified against each town's
      -- UC_LUT: left(code,3)='101' and a "Single family%" description select
      -- identical row sets in Newton, Wayland and Wellesley.
      SELECT *, left(trim(USE_CODE), 3) AS uc, trim(USE_CODE) AS use_code_full
      FROM ST_Read('{assess}')
      WHERE LOC_ID IS NOT NULL AND LOC_ID <> ''
      -- Weston ships three literally duplicated assessor rows; without this
      -- they double-count in every percentile.
      QUALIFY row_number() OVER (PARTITION BY LOC_ID, PROP_ID
                                 ORDER BY TOTAL_VAL DESC) = 1;

    -- Condo/exempt filtering happens at the POLYGON level: if any record
    -- stacked on a parcel is a condo, every record on it carries a fractional
    -- land value and the whole polygon is unusable for a land-ratio screen.
    CREATE OR REPLACE TEMP VIEW bad_parcels AS
      SELECT DISTINCT LOC_ID FROM asr WHERE {excl_pred};
    """)

    if uc_lut:
        # The LUT carries both statewide rows (TOWN_ID = 0) and town-specific
        # overrides; the town's own wording wins where both exist.
        con.execute(f"""
        CREATE OR REPLACE TEMP VIEW uclut AS
          SELECT trim(USE_CODE) AS code,
                 coalesce(any_value(USE_DESC) FILTER (WHERE TOWN_ID <> 0),
                          any_value(USE_DESC)) AS use_desc
          FROM ST_Read('{uc_lut}') GROUP BY 1;
        """)

    con.execute(f"""
    INSERT INTO parcel_geom
      SELECT DISTINCT ON (t.loc_id) t.loc_id, t.geom
      FROM tp t
      WHERE t.loc_id IN (SELECT LOC_ID FROM asr WHERE uc IN ({include}))
        AND t.loc_id NOT IN (SELECT LOC_ID FROM bad_parcels);

    INSERT INTO raw_parcels BY NAME
    SELECT
      '{town.title()}'          AS town,
      a.TOWN_ID                 AS town_id,
      '{release}'               AS release,
      a.LOC_ID                  AS loc_id,
      a.PROP_ID                 AS prop_id,
      a.FY                      AS fy,
      a.uc                      AS use_code,
      {"l.use_desc" if uc_lut else "NULL::VARCHAR"} AS use_desc,
      nullif(trim(a.SITE_ADDR), '')  AS site_addr,
      nullif(trim(a.OWNER1), '')     AS owner1,
      nullif(trim(a.OWN_ADDR), '')   AS own_addr,
      nullif(trim(a.OWN_CITY), '')   AS own_city,
      nullif(trim(a.CITY), '')       AS city,
      nullif(trim(a.OWN_STATE), '')  AS own_state,
      nullif(trim(a.ZONING), '')     AS zoning,
      nullif(trim(a.STYLE), '')      AS style,
      a.LAND_VAL::BIGINT        AS land_val,
      a.BLDG_VAL::BIGINT        AS bldg_val,
      a.OTHER_VAL::BIGINT       AS other_val,
      a.TOTAL_VAL::BIGINT       AS total_val,
      -- DBF numerics encode "missing" as 0, not NULL (standard v3 Appendix A
      -- note 7). Springfield, for one, ships BLD_AREA = 0 for every single
      -- family, so 0 has to become NULL here or it poisons every ratio.
      nullif(a.YEAR_BUILT, 0)   AS year_built,
      nullif(a.RES_AREA, 0)     AS res_area,
      nullif(a.BLD_AREA, 0)     AS bld_area,
      nullif(a.LS_PRICE, 0)     AS ls_price,
      -- LOT_SIZE is N(11,2) whose unit is carried separately in LOT_UNITS.
      CASE WHEN a.LOT_SIZE IS NULL OR a.LOT_SIZE = 0 THEN NULL
           WHEN upper(a.LOT_UNITS) = 'A' THEN a.LOT_SIZE
           WHEN upper(a.LOT_UNITS) = 'S' THEN a.LOT_SIZE / {SQ_FT_PER_ACRE}
      END                       AS lot_acres,
      -- LS_DATE is C(8) 'YYYYMMDD', not a date type. Dates before 1950 are
      -- assessor sentinels (226 rows land on exactly 1900-01-01), not sales.
      CASE WHEN try_strptime(nullif(trim(a.LS_DATE), ''), '%Y%m%d')::DATE
                >= DATE '1950-01-01'
           THEN try_strptime(trim(a.LS_DATE), '%Y%m%d')::DATE END AS ls_date,
      t.shape_area_m2 / {SQ_M_PER_ACRE} AS gis_acres,
      t.x26986, t.y26986,
      count(*) OVER (PARTITION BY a.LOC_ID) AS n_records_on_parcel
    FROM asr a
    JOIN tp t ON t.loc_id = a.LOC_ID
    {"LEFT JOIN uclut l ON l.code = a.use_code_full" if uc_lut else ""}
    WHERE a.uc IN ({include})
      AND a.LOC_ID NOT IN (SELECT LOC_ID FROM bad_parcels)
    """)

    if uc_lut:
        # Guard against a town that zero-pads instead ('0101' meaning 101),
        # which would make left(code,3) pick the wrong class.
        odd = con.execute(f"""
          SELECT count(*) FROM asr a JOIN uclut l ON l.code = a.use_code_full
          WHERE lower(l.use_desc) LIKE 'single family%' AND a.uc NOT IN ({include})
        """).fetchone()[0]
        if odd:
            log(f"  note: {odd} rows in {town} are described as single family "
                f"but their 3-char class is not in {cfg['filters']['include_use_codes']}")

    return con.execute("SELECT count(*) FROM raw_parcels WHERE town = ?",
                       [town.title()]).fetchone()[0]


def fetch_layer(key: str, raw: Path) -> Path:
    """Download and unzip one statewide constraint layer, cached on disk."""
    stem, shp = LAYER_SOURCES[key]
    dest = raw / "layers" / stem
    if not dest.exists():
        zpath = raw / "layers" / f"{stem}.zip"
        zpath.parent.mkdir(parents=True, exist_ok=True)
        if not zpath.exists():
            log(f"  downloading {stem}.zip")
            tmp = zpath.with_suffix(".part")
            urllib.request.urlretrieve(
                f"{BUCKET}/shapefiles/state/{stem}.zip", tmp)
            tmp.rename(zpath)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
    found = list(dest.rglob(shp))
    if not found:
        raise SystemExit(f"{shp} not found in {dest}")
    return found[0]


def spatial_flags(con, cfg):
    """Pre-flag parcels against wetlands, sewer, flood and ACEC, in 26986."""
    con.execute("""
    CREATE OR REPLACE TABLE parcel_flags (
      loc_id VARCHAR, flag_wetland_m DOUBLE, flag_sewer BOOLEAN,
      flag_flood VARCHAR, flag_acec VARCHAR)
    """)
    if not cfg.get("flags", {}).get("enabled"):
        return
    raw = cfg["_raw"]
    radius = float(cfg["flags"]["wetland_search_m"])

    # Every layer is statewide; clip to the parcels' extent first so the joins
    # stay small. Padded by the search radius so nothing just outside is missed.
    b = con.execute("""
      SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
      FROM (SELECT ST_Extent(ST_Union_Agg(geom)) AS e FROM parcel_geom)
    """).fetchone()
    pad = radius + 50
    env = (f"ST_MakeEnvelope({b[0] - pad}, {b[1] - pad}, "
           f"{b[2] + pad}, {b[3] + pad})")

    def clip(key, cols):
        path = fetch_layer(key, raw)
        con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lyr_{key} AS
          SELECT geom, {cols} FROM ST_Read('{path}')
          WHERE ST_Intersects_Extent(geom, {env})
        """)
        return con.execute(f"SELECT count(*) FROM lyr_{key}").fetchone()[0]

    log(f"  clipping constraint layers to the target extent (pad {pad:.0f} m)")
    for key, cols in [
        ("wetland", "1 AS one"),
        ("sewer", "TOWN_ID::INTEGER AS town_id"),
        # Zone X is minimal risk; flagging it would mark half the state.
        ("flood", "FLD_ZONE AS zone, SFHA_TF AS sfha"),
        ("acec", "NAME AS name"),
    ]:
        log(f"    {key}: {clip(key, cols):,} features")

    con.execute(f"""
    CREATE OR REPLACE TABLE parcel_flags AS
    WITH g AS (SELECT loc_id, geom FROM parcel_geom),
    wet AS (
      SELECT g.loc_id, min(ST_Distance(g.geom, w.geom)) AS m
      FROM g JOIN lyr_wetland w ON ST_DWithin(g.geom, w.geom, {radius})
      GROUP BY 1),
    -- A town absent from the sewer layer is unmapped, not unsewered, so its
    -- parcels get NULL rather than false.
    mapped AS (SELECT DISTINCT town_id FROM lyr_sewer),
    sew AS (
      SELECT DISTINCT g.loc_id FROM g JOIN lyr_sewer s
        ON ST_Intersects(g.geom, s.geom)),
    fld AS (
      SELECT g.loc_id, string_agg(DISTINCT f.zone, '/' ORDER BY f.zone) AS zone
      FROM g JOIN lyr_flood f ON ST_Intersects(g.geom, f.geom)
      WHERE f.sfha = 'T' GROUP BY 1),
    ac AS (
      SELECT g.loc_id, any_value(a.name) AS name
      FROM g JOIN lyr_acec a ON ST_Intersects(g.geom, a.geom) GROUP BY 1)
    SELECT p.loc_id,
           wet.m AS flag_wetland_m,
           CASE WHEN p.town_id IN (SELECT town_id FROM mapped)
                THEN sew.loc_id IS NOT NULL END AS flag_sewer,
           fld.zone AS flag_flood,
           ac.name  AS flag_acec
    FROM (SELECT DISTINCT loc_id, town_id FROM raw_parcels) p
    LEFT JOIN wet USING (loc_id)
    LEFT JOIN sew ON sew.loc_id = p.loc_id
    LEFT JOIN fld USING (loc_id)
    LEFT JOIN ac  USING (loc_id)
    """)
    s = con.execute("""
      SELECT count(*) FILTER (WHERE flag_wetland_m IS NOT NULL),
             count(*) FILTER (WHERE flag_wetland_m = 0),
             count(*) FILTER (WHERE flag_sewer),
             count(*) FILTER (WHERE flag_sewer IS NULL),
             count(*) FILTER (WHERE flag_flood IS NOT NULL),
             count(*) FILTER (WHERE flag_acec IS NOT NULL)
      FROM parcel_flags""").fetchone()
    log(f"  wetland within {radius:.0f} m: {s[0]:,} ({s[1]:,} overlapping) · "
        f"on sewer: {s[2]:,} ({s[3]:,} unmapped) · "
        f"FEMA flood zone: {s[4]:,} · ACEC: {s[5]:,}")


def completeness(con, out: Path, name="parcels") -> dict[tuple[str, str], float]:
    """Per town x field non-missing rate. Drives per-town component dropping."""
    sel = ", ".join(f"count({f})::DOUBLE / count(*) AS {f}" for f in COMPLETENESS_FIELDS)
    cur = con.execute(
        f"SELECT town, count(*) AS n_parcels, {sel},"
        " avg(bldg_val_missing::INT) AS structure_value_suppressed"
        " FROM comp GROUP BY town ORDER BY town")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    lookup, report = {}, [",".join(cols)]
    for r in rows:
        report.append(",".join(f"{v:.4f}" if isinstance(v, float) else str(v) for v in r))
        for c, v in zip(cols[2:], r[2:]):
            lookup[(r[0], c)] = v
    (out / f"{name}_completeness.csv").write_text("\n".join(report) + "\n")
    return lookup


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def age_fit(year: np.ndarray, cfg) -> np.ndarray:
    """Raw age signal, higher is better.

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


def score_parcels(con, cfg, cov, out: Path, name="parcels", quiet=False):
    """Components -> neighbourhood aggregates -> percentiles -> composite."""
    cur = con.execute("""
      SELECT row_id, town, x26986, y26986, res_area, year_built, lot_acres,
             land_ratio, impr_psf, far, years_since_sale, min_lot_acres,
             absentee
      FROM comp ORDER BY row_id
    """)
    d = cur.fetchnumpy()
    n = len(d["row_id"])
    towns = np.asarray(d["town"], dtype=object)
    quiet or log(f"{n} parcels across {len(np.unique(towns))} towns")

    def col(name):
        return np.ma.filled(np.ma.asarray(d[name]).astype("float64"), np.nan)

    xy = np.column_stack([col("x26986"), col("y26986")])
    idx = knn_index(towns, xy, min(cfg["neighborhood"]["k"], n))

    res = col("res_area")
    with np.errstate(invalid="ignore"):
        nbhd_med_res_area = np.nanmedian(res[idx], axis=1)
        vals = {
            "land_ratio": col("land_ratio"),
            "impr_psf": col("impr_psf"),
            "age_fit": age_fit(col("year_built"), cfg),
            "far": col("far"),
            "area_vs_nbhd": res / nbhd_med_res_area,
            "lot_vs_zoning_min": col("lot_acres") / col("min_lot_acres"),
            "years_since_sale": col("years_since_sale"),
        }

    floor = cfg["completeness"]["min_town_coverage"]
    for comp_name, arr in vals.items():
        src = SOURCE_FIELD[comp_name]
        for t in np.unique(towns):
            m = towns == t
            if cov.get((t, src), 1.0) < floor and not np.isnan(arr[m]).all():
                quiet or log(f"  dropping {comp_name} for {t}: "
                             f"{src} coverage {cov[(t, src)]:.1%} < {floor:.0%}")
                arr[m] = np.nan

    pct_town = {c: pct_rank(vals[c], towns, dr) for c, dr in COMPONENTS.items()}
    pct_nbr = {c: nbr_pct_rank(vals[c], idx, dr) for c, dr in COMPONENTS.items()}
    scoring = pct_town if cfg["run"]["percentile_scope"] == "town" else pct_nbr

    w = cfg["weights"]
    absentee = np.ma.filled(np.ma.asarray(d["absentee"]).astype("float64"), 0.0)
    num, den = np.zeros(n), np.zeros(n)
    for c in COMPONENTS:
        wc = w["age" if c == "age_fit" else c]
        p = scoring[c]
        ok = ~np.isnan(p)
        num[ok] += wc * p[ok]
        den[ok] += wc
    num += w["absentee"] * absentee
    den += w["absentee"]
    # Missing components lose their weight rather than scoring zero, so a town
    # that did not populate a column is not uniformly pushed to the bottom.
    score = np.where(den > 0, num / den, np.nan)

    arrays = {
        "row_id": np.asarray(d["row_id"], dtype="int64"),
        "score": score,
        "score_coverage": den / sum(w.values()),
        "nbhd_med_res_area": nbhd_med_res_area,
    }
    for c in COMPONENTS:
        arrays[f"val_{c}"] = vals[c]
        arrays[f"pct_{c}"] = pct_town[c]
        arrays[f"nbr_{c}"] = pct_nbr[c]
    con.register("scores", arrays)

    parquet = out / f"{name}.parquet"
    con.execute(f"""
    COPY (
      SELECT c.town, c.town_id, c.release, c.fy, c.loc_id, c.prop_id,
             c.site_addr, c.city, c.owner1, c.own_addr, c.own_city, c.own_state,
             c.use_code, c.use_desc, c.zoning, c.style,
             c.land_val, c.bldg_val, c.other_val, c.total_val,
             c.year_built, c.res_area, c.bld_area, c.lot_acres, c.gis_acres,
             c.ls_date, c.ls_price, c.min_lot_acres, c.min_frontage_ft,
             c.n_records_on_parcel, c.absentee, c.bldg_val_missing,
             c.flag_wetland_m, c.flag_sewer, c.flag_flood, c.flag_acec,
             s.* EXCLUDE (row_id),
             ST_X(ST_Transform(ST_Point(c.x26986, c.y26986),
                  'EPSG:26986', 'EPSG:4326', always_xy := true)) AS lon,
             ST_Y(ST_Transform(ST_Point(c.x26986, c.y26986),
                  'EPSG:26986', 'EPSG:4326', always_xy := true)) AS lat
      FROM comp c JOIN scores s USING (row_id)
      ORDER BY s.score DESC NULLS LAST
    ) TO '{parquet}' (FORMAT parquet, COMPRESSION zstd)
    """)
    quiet or log(f"wrote {parquet} ({parquet.stat().st_size / 1e6:.1f} MB)")
    return parquet


def build_components(con, cfg_dir: Path, quiet=False):
    """Raw per-parcel signals. No ranking yet."""
    zmin = cfg_dir / "zoning_minimums.csv"
    if zmin.exists():
        con.execute(f"CREATE OR REPLACE TEMP VIEW zmin AS SELECT * FROM read_csv_auto('{zmin}')")
        zjoin = ("LEFT JOIN zmin z ON upper(z.town) = upper(p.town) "
                 "AND upper(z.zoning) = upper(p.zoning)")
        zcols = "z.min_lot_acres::DOUBLE AS min_lot_acres, z.min_frontage_ft::DOUBLE AS min_frontage_ft"
    else:
        zjoin = ""
        zcols = "NULL::DOUBLE AS min_lot_acres, NULL::DOUBLE AS min_frontage_ft"
        quiet or log("no zoning_minimums.csv: lot_vs_zoning_min is null everywhere. Its "
            "weight is redistributed; watch score_coverage.")

    con.execute(f"""
    CREATE OR REPLACE TABLE comp AS
    SELECT row_number() OVER (ORDER BY p.town, p.loc_id, p.prop_id) AS row_id,
           p.* EXCLUDE (gis_acres), {zcols},
           fl.flag_wetland_m, fl.flag_sewer, fl.flag_flood, fl.flag_acec,
      -- One polygon carrying several surviving single-family records means the
      -- GIS area is shared and cannot be attributed to any one of them.
      CASE WHEN p.n_records_on_parcel = 1 THEN p.gis_acres END AS gis_acres,
      -- A standing structure with living area and a year built cannot really
      -- be assessed at $0; Newton ships ~10% of its single families that way.
      -- Left alone it reads as land_ratio = 1.0 and impr_psf = 0, which pins
      -- those records at the top of the two heaviest components. Treat the
      -- structure value as missing instead and let the weight redistribute.
      (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
                                                                AS bldg_val_missing,
      CASE WHEN p.total_val > 0 AND p.land_val > 0
                AND NOT (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
           THEN p.land_val::DOUBLE / p.total_val END            AS land_ratio,
      CASE WHEN p.res_area > 0
                AND NOT (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
           THEN p.bldg_val::DOUBLE / p.res_area END             AS impr_psf,
      -- Structure vs lot. BLD_AREA (gross) is preferred but some towns ship it
      -- as all zeros, so RES_AREA is the fallback.
      CASE WHEN p.lot_acres > 0
           THEN coalesce(p.bld_area, p.res_area)::DOUBLE
                / (p.lot_acres * {SQ_FT_PER_ACRE}) END          AS far,
      date_diff('year', p.ls_date, current_date)::DOUBLE        AS years_since_sale,
      -- Absentee: mailing address materially different from the property.
      -- Compared on street number and the first 5 characters of the street
      -- name, so "TAVERN CIRCLE" vs "TAVERN CIR" is not a false hit.
      -- Deliberately NOT compared on city: Newton's CITY column holds village
      -- names (NEWTON CENTRE, WEST NEWTON, AUBURNDALE) while OWN_CITY usually
      -- says plain NEWTON, which alone produced a 55% false-positive rate.
      (p.own_addr IS NOT NULL AND p.site_addr IS NOT NULL AND (
          upper(coalesce(p.own_state, 'MA')) <> 'MA'
       OR regexp_extract(upper(p.own_addr), '^[0-9]+')
          <> regexp_extract(upper(p.site_addr), '^[0-9]+')
       OR left(regexp_replace(upper(p.own_addr), '^[0-9A-Z\\-]*[0-9]\\s+', ''), 5)
          <> left(regexp_replace(upper(p.site_addr), '^[0-9A-Z\\-]*[0-9]\\s+', ''), 5)
      ))                                                        AS absentee
    FROM raw_parcels p
    LEFT JOIN parcel_flags fl USING (loc_id)
    {zjoin}
    """)


def build_tiles(con, cfg, out: Path, parquet: Path):
    """Reproject to 4326 once, here, and hand geometry to tippecanoe."""
    if not shutil.which("tippecanoe"):
        log("tippecanoe not on PATH: skipping tiles (brew install tippecanoe)")
        return
    geojsonl = out / "parcels.geojsonl"
    con.execute(f"""
    COPY (
      SELECT ST_Transform(g.geom, 'EPSG:26986', 'EPSG:4326', always_xy := true) AS geom,
             p.loc_id, round(p.score, 4) AS score
      FROM parcel_geom g JOIN read_parquet('{parquet}') p USING (loc_id)
    ) TO '{geojsonl}' (FORMAT gdal, DRIVER 'GeoJSONSeq')
    """)
    pmtiles = out / "parcels.pmtiles"
    subprocess.run([
        "tippecanoe", "-o", str(pmtiles), "--force", "-l", "parcels",
        "-Z", str(cfg["tiles"]["min_zoom"]), "-z", str(cfg["tiles"]["max_zoom"]),
        "--drop-densest-as-needed", "--extend-zooms-if-still-dropping",
        "--simplification=4", str(geojsonl),
    ], check=True)
    geojsonl.unlink()
    log(f"wrote {pmtiles} ({pmtiles.stat().st_size / 1e6:.1f} MB)")


def load_config(cfg_path="config.toml"):
    cfg_file = Path(cfg_path).resolve()
    cfg = tomllib.loads(cfg_file.read_text())
    cfg["_dir"] = cfg_file.parent
    cfg["_raw"] = (cfg_file.parent / cfg["run"]["raw_dir"]).resolve()
    cfg["_out"] = (cfg_file.parent / cfg["run"]["out_dir"]).resolve()
    cfg["_raw"].mkdir(parents=True, exist_ok=True)
    cfg["_out"].mkdir(parents=True, exist_ok=True)
    return cfg


def run(cfg, assess_override: dict[str, Path] | None = None,
        name="parcels", tiles=True, quiet=False, flags=True):
    """One full scoring pass. Returns (connection, parquet path).

    assess_override maps town -> Assess .dbf, used to re-score a prior vintage.
    """
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"CREATE TABLE raw_parcels ({RAW_COLUMNS})")
    con.execute("CREATE TABLE parcel_geom (loc_id VARCHAR, geom GEOMETRY)")

    keys = town_keys(cfg["_raw"])
    for town in cfg["run"]["towns"]:
        d = fetch_town(town, keys, cfg["_raw"])
        n = load_town(con, town, d, cfg, (assess_override or {}).get(town))
        if not quiet:
            log(f"{town}: {n} scored parcels")

    if flags:
        spatial_flags(con, cfg)
    else:
        con.execute("""CREATE OR REPLACE TABLE parcel_flags (
          loc_id VARCHAR, flag_wetland_m DOUBLE, flag_sewer BOOLEAN,
          flag_flood VARCHAR, flag_acec VARCHAR)""")
    build_components(con, cfg["_dir"], quiet=quiet)
    cov = completeness(con, cfg["_out"], name=name)
    parquet = score_parcels(con, cfg, cov, cfg["_out"], name=name, quiet=quiet)
    if tiles:
        build_tiles(con, cfg, cfg["_out"], parquet)
    return con, parquet


def main(cfg_path="config.toml"):
    con, _ = run(load_config(cfg_path))
    con.close()


def _selfcheck():
    """Smallest thing that fails if the ranking or age-band logic breaks."""
    cfg = {"age": {"mode": "band", "peak_lo": 1945, "peak_hi": 1975,
                   "falloff_years": 35}}
    a = age_fit(np.array([1900.0, 1945, 1960, 1975, 2010, np.nan]), cfg)
    assert a[1] == a[2] == a[3] == 1.0, a
    assert a[0] == 0.0 and a[4] == 0.0, a       # exactly falloff_years outside
    assert np.isnan(a[5])
    mid = age_fit(np.array([1960.0 - 0.0, 1927.5, 1992.5]), cfg)
    assert abs(mid[1] - 0.5) < 1e-9 and abs(mid[2] - 0.5) < 1e-9, mid
    cfg["age"]["mode"] = "older_better"
    m = age_fit(np.array([1900.0, 2000.0]), cfg)
    assert m[0] > m[1]

    g = np.array(["a", "a", "a", "b", "b"], dtype=object)
    v = np.array([1.0, 2.0, 3.0, np.nan, 5.0])
    hi = pct_rank(v, g, "hi")
    assert list(hi[:3]) == [0.0, 0.5, 1.0], hi
    assert np.isnan(hi[3]) and hi[4] == 0.5     # lone non-null in its group
    lo = pct_rank(v, g, "lo")
    assert list(lo[:3]) == [1.0, 0.5, 0.0], lo  # direction inverts the ranking

    # A missing component must cost its weight, not score zero.
    idx = knn_index(np.array(["a"] * 4, dtype=object),
                    np.array([[0.0, 0], [1, 0], [2, 0], [100, 0]]), 3)
    assert idx.shape == (4, 3) and idx[0, 0] == 0
    assert set(idx[3]) == {3, 2, 1}, idx[3]     # nearest are the closest three
    nb = nbr_pct_rank(np.array([1.0, 2.0, 3.0, np.nan]), idx, "hi")
    assert np.isnan(nb[3]) and nb[0] < nb[2]
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main(*[a for a in sys.argv[1:] if not a.startswith("-")])

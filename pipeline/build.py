#!/usr/bin/env python3
"""MassGIS Level 3 parcels -> scored parcels.parquet + parcels.pmtiles.

Answers "which parcels are the best complete teardown-and-rebuild projects",
which is a different question from "which houses look likely to be demolished".
The scoring itself lives in model.py; this file's job is to assemble authoritative
inputs, be honest about which ones are missing, and write the artifacts.

Idempotent and resumable: downloaded town archives are cached in raw_dir and
reused. Delete a town's zip to force a re-fetch.

All areas, distances and neighbourhoods are computed in EPSG:26986
(Mass State Plane Mainland, metres). Reprojection to 4326 happens once, at the
tile-writing step.

Usage:  uv run build.py [config.toml]
        uv run build.py --selfcheck
"""

import json
import math
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

import model
from model import (ACQUISITION, AUTHORITATIVE, ESTIMATED, MISSING,
                   OBSOLESCENCE, SOURCE_FIELD, SQ_FT_PER_ACRE)

BUCKET = "https://s3.us-east-1.amazonaws.com/download.massgis.digital.mass.gov"
PREFIX = "shapefiles/l3parcels/"
SQ_M_PER_ACRE = 4046.8564224
M_PER_FT = 0.3048

# Reported per town. Measured on `comp`, i.e. after 0-means-missing and the
# sentinel-date and $0-structure corrections, so the numbers are what scoring
# actually sees rather than what the DBF nominally contains.
COMPLETENESS_FIELDS = [
    "land_ratio", "impr_psf", "far", "years_since_sale", "year_built",
    "res_area", "bld_area", "lot_acres", "gis_acres", "ls_price", "owner1",
    "min_lot_acres", "max_far", "zoning", "own_addr", "listing_status",
    "days_on_market", "existing_sqft", "lot_compactness", "fp_area_sqft",
]

# MassGIS layers, all published in EPSG:26986 so no reprojection is needed
# before measuring.
LAYER_SOURCES = {
    "wetland": ("wetlandsdep_poly", "WETLANDSDEP_POLY.shp"),
    # POTW = publicly owned treatment works, i.e. municipal sewer. The NONPOTW
    # layer in the same archive is private package plants serving one campus or
    # condo, which is not what "is this house on town sewer" means.
    "sewer": ("DEP_Sewer_Service_Areas", "SEWER_SERVICE_AREA_POTW_POLY.shp"),
    "flood": ("nfhl", "FEMA_NFHL_POLY.shp"),
    "acec": ("acecs", "acecs_poly.shp"),
    # MHC cultural resource inventory. Points are individual inventoried
    # buildings; polygons are historic areas and districts.
    "historic_pt": ("mhcinv", "MHCINV_PT.shp"),
    "historic_poly": ("mhcinv", "MHCINV_POLY.shp"),
    # Building footprints. Statewide coverage is PARTIAL — absence is not
    # evidence of a vacant lot, so the columns go null and confidence drops.
    "footprint": ("buildingfp_poly", "BUILDINGFP_POLY.shp"),
}

RAW_COLUMNS = """
  town VARCHAR, town_id INTEGER, release VARCHAR, loc_id VARCHAR,
  prop_id VARCHAR, fy INTEGER, use_code VARCHAR, use_desc VARCHAR,
  site_addr VARCHAR, owner1 VARCHAR, own_addr VARCHAR, own_city VARCHAR,
  own_co VARCHAR, city VARCHAR, own_state VARCHAR, zoning VARCHAR,
  style VARCHAR, stories VARCHAR, num_rooms INTEGER, units INTEGER,
  land_val BIGINT, bldg_val BIGINT, other_val BIGINT, total_val BIGINT,
  year_built INTEGER, res_area BIGINT, bld_area BIGINT, ls_price BIGINT,
  lot_acres DOUBLE, ls_date DATE, gis_acres DOUBLE,
  lot_compactness DOUBLE, lot_width_ft DOUBLE, lot_depth_ft DOUBLE,
  x26986 DOUBLE, y26986 DOUBLE, n_records_on_parcel BIGINT
"""

# Optional local inputs, all read from the config directory. Every one of them
# is absent by default and the build works without it; each absence costs data
# confidence rather than silently defaulting to a favourable value.
OPTIONAL_INPUTS = {
    # town,loc_id,zoning,authority,source_url,as_of. Spatial assignments from
    # official municipal zoning polygons override assessor transcriptions.
    "zoning_assignments": ("zoning_assignments.csv", "build",
                           "parcel-to-district assignments from official GIS"),
    # town,zoning,min_lot_acres,min_frontage_ft,max_far,max_lot_coverage,
    # authority,source_url,as_of
    "zoning_minimums": ("zoning_minimums.csv", "build",
                        "authoritative zoning dimensional limits"),
    # town,loc_id,site_addr,status,list_price,original_list_price,list_date,
    # days_on_market
    "listings": ("listings.csv", "build",
                 "live list price, days on market, price cuts"),
    # town,loc_id,site_addr,permit_date,permit_type (demolition|new_dwelling)
    "permits": ("permits.csv", "calibrate",
                "demolition permits joined to new-dwelling permits"),
    # legacy single-purpose name, still honoured
    "demolition_permits": ("demolition_permits.csv", "calibrate",
                           "demolition permits only (superseded by permits.csv)"),
}


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
             ST_Perimeter(geom) AS shape_perim_m,
             ST_XMax(geom) - ST_XMin(geom) AS bbox_w_m,
             ST_YMax(geom) - ST_YMin(geom) AS bbox_h_m,
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
      nullif(trim(a.OWN_CO), '')     AS own_co,
      nullif(trim(a.CITY), '')       AS city,
      nullif(trim(a.OWN_STATE), '')  AS own_state,
      nullif(trim(a.ZONING), '')     AS zoning,
      nullif(trim(a.STYLE), '')      AS style,
      nullif(trim(a.STORIES), '')    AS stories,
      nullif(a.NUM_ROOMS, 0)    AS num_rooms,
      nullif(a.UNITS, 0)        AS units,
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
      -- Lot shape. 4*pi*A/P^2: a circle is 1.0, a square 0.785, a 4:1
      -- rectangle 0.50. Catches slivers, flag lots and pipestems, whose
      -- buildable envelope is nothing like their acreage suggests.
      CASE WHEN t.shape_perim_m > 0
           THEN 4 * pi() * t.shape_area_m2 / (t.shape_perim_m * t.shape_perim_m)
      END                       AS lot_compactness,
      -- Axis-aligned bounding box, narrower side first. A proxy for frontage,
      -- which the assessor extract does not carry, and NOT frontage: a corner
      -- lot or a diagonal street widens the box past the street line.
      least(t.bbox_w_m, t.bbox_h_m) / {M_PER_FT}    AS lot_width_ft,
      greatest(t.bbox_w_m, t.bbox_h_m) / {M_PER_FT} AS lot_depth_ft,
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


FLAG_COLUMNS = """
  loc_id VARCHAR, flag_wetland_m DOUBLE, flag_sewer BOOLEAN,
  flag_flood VARCHAR, flag_acec VARCHAR, flag_historic VARCHAR,
  flag_historic_area VARCHAR, flag_historic_designations VARCHAR,
  fp_area_sqft DOUBLE, fp_count BIGINT, fp_min_setback_ft DOUBLE
"""


def spatial_flags(con, cfg):
    """Screen parcels against wetlands, sewer, flood, ACEC, MHC and footprints.

    Every one of these now feeds rebuild_feasibility_score or the buildable
    envelope, so a NULL matters: it is "nobody looked", not "clear".
    """
    con.execute(f"CREATE OR REPLACE TABLE parcel_flags ({FLAG_COLUMNS})")
    if not cfg.get("flags", {}).get("enabled"):
        return
    raw = cfg["_raw"]
    radius = float(cfg["flags"]["wetland_search_m"])
    want_fp = bool(cfg["flags"].get("footprints", True))
    want_hist = bool(cfg["flags"].get("historic", True))

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

    layers = [
        ("wetland", "1 AS one"),
        ("sewer", "TOWN_ID::INTEGER AS town_id"),
        # Zone X is minimal risk; flagging it would mark half the state.
        ("flood", "FLD_ZONE AS zone, SFHA_TF AS sfha"),
        ("acec", "NAME AS name"),
    ]
    if want_hist:
        layers += [("historic_pt", "LISTING AS listing, TYPE AS kind, "
                                   "DESIGNATNS AS designations"),
                   ("historic_poly", "LISTING AS listing, TYPE AS kind, "
                                     "DESIGNATNS AS designations, "
                                     "HIST_NAME AS name")]
    if want_fp:
        layers.append(("footprint", "1 AS one"))

    log(f"  clipping constraint layers to the target extent (pad {pad:.0f} m)")
    for key, cols in layers:
        log(f"    {key}: {clip(key, cols):,} features")

    if not want_hist:
        con.execute("CREATE OR REPLACE TEMP TABLE lyr_historic_pt "
                    "(geom GEOMETRY, listing VARCHAR, kind VARCHAR, "
                    " designations VARCHAR)")
        con.execute("CREATE OR REPLACE TEMP TABLE lyr_historic_poly "
                    "(geom GEOMETRY, listing VARCHAR, kind VARCHAR, "
                    " designations VARCHAR, name VARCHAR)")
    if not want_fp:
        con.execute("CREATE OR REPLACE TEMP TABLE lyr_footprint "
                    "(geom GEOMETRY, one INTEGER)")

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
      FROM g JOIN lyr_acec a ON ST_Intersects(g.geom, a.geom) GROUP BY 1),
    -- An inventoried building whose point falls on the parcel. MHC coverage is
    -- very uneven (Newton: 155 points for 17k single families), so a parcel
    -- with no point is NOT cleared, it is simply unexamined.
    hist AS (
      SELECT g.loc_id,
             string_agg(DISTINCT h.listing, '/' ORDER BY h.listing) AS listing,
             string_agg(DISTINCT coalesce(h.designations, ''), ' ') AS designations
      FROM g JOIN lyr_historic_pt h ON ST_Intersects(g.geom, h.geom)
      WHERE h.kind IN ('Building', 'Structure') GROUP BY 1),
    harea AS (
      SELECT g.loc_id, any_value(a.name) AS name,
             string_agg(DISTINCT coalesce(a.designations, ''), ' ') AS designations
      FROM g JOIN lyr_historic_poly a ON ST_Intersects(g.geom, a.geom)
      GROUP BY 1),
    -- Footprints are attributed by centroid containment: one pass, and a
    -- garage straddling a lot line lands on exactly one parcel.
    fpj AS (
      SELECT g.loc_id, count(*) AS n,
             sum(ST_Area(f.geom)) / {M_PER_FT * M_PER_FT} AS area_sqft,
             min(ST_Distance(f.geom, ST_ExteriorRing(g.geom))) / {M_PER_FT}
               AS setback_ft
      FROM g JOIN lyr_footprint f ON ST_Contains(g.geom, ST_Centroid(f.geom))
      GROUP BY 1)
    SELECT p.loc_id,
           wet.m AS flag_wetland_m,
           CASE WHEN p.town_id IN (SELECT town_id FROM mapped)
                THEN sew.loc_id IS NOT NULL END AS flag_sewer,
           fld.zone AS flag_flood,
           ac.name  AS flag_acec,
           hist.listing AS flag_historic,
           harea.name   AS flag_historic_area,
           nullif(trim(coalesce(hist.designations, '') || ' '
                       || coalesce(harea.designations, '')), '')
             AS flag_historic_designations,
           fpj.area_sqft AS fp_area_sqft,
           fpj.n         AS fp_count,
           fpj.setback_ft AS fp_min_setback_ft
    FROM (SELECT DISTINCT loc_id, town_id FROM raw_parcels) p
    LEFT JOIN wet USING (loc_id)
    LEFT JOIN sew ON sew.loc_id = p.loc_id
    LEFT JOIN fld USING (loc_id)
    LEFT JOIN ac  USING (loc_id)
    LEFT JOIN hist USING (loc_id)
    LEFT JOIN harea USING (loc_id)
    LEFT JOIN fpj USING (loc_id)
    """)
    s = con.execute("""
      SELECT count(*) FILTER (WHERE flag_wetland_m IS NOT NULL),
             count(*) FILTER (WHERE flag_wetland_m = 0),
             count(*) FILTER (WHERE flag_sewer),
             count(*) FILTER (WHERE flag_sewer IS NULL),
             count(*) FILTER (WHERE flag_flood IS NOT NULL),
             count(*) FILTER (WHERE flag_acec IS NOT NULL),
             count(*) FILTER (WHERE flag_historic IS NOT NULL),
             count(*) FILTER (WHERE flag_historic_area IS NOT NULL),
             count(*) FILTER (WHERE fp_area_sqft IS NOT NULL)
      FROM parcel_flags""").fetchone()
    log(f"  wetland within {radius:.0f} m: {s[0]:,} ({s[1]:,} overlapping) · "
        f"on sewer: {s[2]:,} ({s[3]:,} unmapped) · flood: {s[4]:,} · "
        f"ACEC: {s[5]:,} · MHC building: {s[6]:,} · MHC area: {s[7]:,} · "
        f"footprint: {s[8]:,}")


def optional_inputs(cfg_dir: Path, quiet=False) -> dict[str, Path]:
    """Which optional local datasets exist. Absence is reported, never filled."""
    found = {}
    for key, (fname, _stage, _why) in OPTIONAL_INPUTS.items():
        p = cfg_dir / fname
        if p.exists():
            found[key] = p
    if not quiet:
        for key, (fname, stage, why) in OPTIONAL_INPUTS.items():
            if key == "demolition_permits" and "permits" in found:
                continue
            state = "present" if key in found else "ABSENT"
            log(f"  optional input {fname:<24} {state:<8} [{stage}] {why}")
        if "zoning_minimums" not in found:
            log("  -> no authoritative zoning: the nonconforming-lot, "
                "subdivision and FAR gates cannot fire and every parcel loses "
                "the zoning share of data_confidence_score. Nothing is assumed "
                "conforming.")
        if "listings" not in found:
            log("  -> no listings export: list price, days on market and price "
                "cuts are null; acquisition price falls back to an assessment "
                "multiple and its four listing components are dropped from "
                "acquisition_likelihood_score.")
    return found


def build_components(con, cfg, inputs: dict[str, Path], quiet=False):
    """Raw per-parcel signals and joined optional inputs. No ranking yet."""
    zassign = inputs.get("zoning_assignments")
    if zassign:
        con.execute(f"""CREATE OR REPLACE TEMP VIEW zassign AS
                        SELECT * FROM read_csv_auto('{zassign}')""")
        have_assign = {c.lower() for c in
                       (r[0] for r in con.execute("DESCRIBE zassign").fetchall())}
        required = {"town", "loc_id", "zoning"}
        missing = required - have_assign
        if missing:
            raise ValueError(f"zoning_assignments.csv missing columns: {sorted(missing)}")
        dupes = con.execute("""SELECT count(*) FROM (
                                 SELECT town, loc_id FROM zassign
                                 GROUP BY 1, 2 HAVING count(*) > 1)""").fetchone()[0]
        if dupes:
            raise ValueError(f"zoning_assignments.csv has {dupes} duplicate parcel keys")
        assign_join = ("LEFT JOIN zassign za ON upper(za.town) = upper(p.town) "
                       "AND za.loc_id = p.loc_id")
        zone_expr = "coalesce(nullif(trim(za.zoning), ''), p.zoning)"
        assignment_authority = ("za.authority" if "authority" in have_assign
                                else "NULL")
    else:
        assign_join = ""
        zone_expr = "p.zoning"
        assignment_authority = "NULL"

    zmin = inputs.get("zoning_minimums")
    if zmin:
        con.execute(f"""CREATE OR REPLACE TEMP VIEW zmin AS
                        SELECT * FROM read_csv_auto('{zmin}')""")
        have = {c.lower() for c in
                (r[0] for r in con.execute("DESCRIBE zmin").fetchall())}
        opt = lambda c: (f"z.{c}::DOUBLE" if c in have else "NULL::DOUBLE")
        zjoin = (f"{assign_join} LEFT JOIN zmin z "
                 f"ON upper(z.town) = upper(p.town) "
                 # Newton appends overlay codes (for example SR2/MRT); the
                 # underlying district still supplies single-family lot area.
                 f"AND upper(z.zoning) = upper(split_part({zone_expr}, '/', 1))")
        zcols = f"""
          {opt('min_lot_acres')} AS min_lot_acres,
          {opt('min_frontage_ft')} AS min_frontage_ft,
          {opt('max_far')} AS max_far,
          {opt('max_lot_coverage')} AS max_lot_coverage,
          coalesce({assignment_authority},
                   {('z.authority' if 'authority' in have else 'NULL')})::VARCHAR
            AS zoning_authority,
          {assignment_authority}::VARCHAR AS zoning_assignment_authority"""
    else:
        zjoin = assign_join
        zcols = """NULL::DOUBLE AS min_lot_acres, NULL::DOUBLE AS min_frontage_ft,
                   NULL::DOUBLE AS max_far, NULL::DOUBLE AS max_lot_coverage,
                   """ + f"""{assignment_authority}::VARCHAR AS zoning_authority,
                   {assignment_authority}::VARCHAR AS zoning_assignment_authority"""

    lst = inputs.get("listings")
    if lst:
        con.execute(f"""CREATE OR REPLACE TEMP VIEW lst AS
                        SELECT * FROM read_csv_auto('{lst}')""")
        have = {c.lower() for c in
                (r[0] for r in con.execute("DESCRIBE lst").fetchall())}
        o = lambda c, t="DOUBLE": (f"l.{c}::{t}" if c in have else f"NULL::{t}")
        ljoin = ("""LEFT JOIN lst l
                    ON upper(l.town) = upper(p.town)
                   AND (l.loc_id = p.loc_id
                        OR upper(l.site_addr) = upper(p.site_addr))""")
        lcols = f"""
          {o('status', 'VARCHAR')} AS listing_status,
          {o('list_price')} AS list_price,
          {o('original_list_price')} AS original_list_price,
          {o('days_on_market')} AS days_on_market"""
    else:
        ljoin = ""
        lcols = """NULL::VARCHAR AS listing_status, NULL::DOUBLE AS list_price,
                   NULL::DOUBLE AS original_list_price,
                   NULL::DOUBLE AS days_on_market"""

    con.execute(f"""
    CREATE OR REPLACE TABLE comp AS
    SELECT row_number() OVER (ORDER BY p.town, p.loc_id, p.prop_id) AS row_id,
           p.* EXCLUDE (gis_acres, zoning), {zone_expr} AS zoning,
           {zcols}, {lcols},
           fl.flag_wetland_m, fl.flag_sewer, fl.flag_flood, fl.flag_acec,
           fl.flag_historic, fl.flag_historic_area,
           fl.flag_historic_designations,
           fl.fp_area_sqft, fl.fp_count, fl.fp_min_setback_ft,
      -- One polygon carrying several surviving single-family records means the
      -- GIS area is shared and cannot be attributed to any one of them.
      CASE WHEN p.n_records_on_parcel = 1 THEN p.gis_acres END AS gis_acres,
      -- A standing structure with living area and a year built cannot really
      -- be assessed at $0; Newton ships ~10% of its single families that way.
      -- Left alone it reads as land_ratio = 1.0 and impr_psf = 0, which pinned
      -- those records at the top of the two heaviest components of the old
      -- score. Treat the structure value as missing and lose the confidence.
      (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
                                                                AS bldg_val_missing,
      CASE WHEN p.total_val > 0 AND p.land_val > 0
                AND NOT (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
           THEN p.land_val::DOUBLE / p.total_val END            AS land_ratio,
      CASE WHEN p.res_area > 0
                AND NOT (p.bldg_val = 0 AND (p.res_area > 0 OR p.year_built IS NOT NULL))
           THEN p.bldg_val::DOUBLE / p.res_area END             AS impr_psf,
      -- Gross building area where the town ships it, living area otherwise.
      coalesce(p.bld_area, p.res_area)::DOUBLE                  AS existing_sqft,
      -- Assessor lot size is preferred; the mapped polygon is the fallback, and
      -- only where one record owns the polygon.
      coalesce(p.lot_acres,
               CASE WHEN p.n_records_on_parcel = 1 THEN p.gis_acres END)
                                                                AS lot_acres_used,
      CASE WHEN p.lot_acres > 0
           THEN coalesce(p.bld_area, p.res_area)::DOUBLE
                / (p.lot_acres * {SQ_FT_PER_ACRE}) END          AS far,
      date_diff('year', p.ls_date, current_date)::DOUBLE        AS years_since_sale,
      year(p.ls_date)::DOUBLE                                   AS ls_year,
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
      ))                                                        AS absentee,
      -- Public-record motivation signals off OWNER1. An estate is settling and
      -- has to sell; a trust is ordinary tax planning and is NOT counted.
      (p.owner1 IS NOT NULL AND regexp_matches(upper(p.owner1),
        '(^|[^A-Z])(EST(ATE)?( OF)?|HEIRS( OF)?|DEVISEES)([^A-Z]|$)'))
                                                                AS estate,
      (p.own_co IS NOT NULL)                                    AS care_of
    FROM raw_parcels p
    LEFT JOIN parcel_flags fl USING (loc_id)
    {zjoin}
    {ljoin}
    """)
    if zmin:
        n = con.execute("SELECT count(*) FROM comp WHERE min_lot_acres IS NOT NULL"
                        ).fetchone()[0]
        quiet or log(f"  zoning minimums matched {n:,} parcels")
    if zassign:
        n = con.execute("""SELECT count(*) FROM raw_parcels p JOIN zassign za
                           ON upper(za.town) = upper(p.town)
                          AND za.loc_id = p.loc_id""").fetchone()[0]
        quiet or log(f"  official GIS zoning assigned {n:,} parcels")


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
# comparable sales
# --------------------------------------------------------------------------

def comparable_sales(d, cfg, towns, xy, quiet=False):
    """Resale value, observed FAR and the acquisition multiple, per town.

    Comparables are new-construction sales taken from the assessor's own
    LS_DATE / LS_PRICE: a parcel whose structure was built recently and which
    sold within a few years of that build is a new-home sale. Public record, no
    scraping, and no synthetic comparable anywhere — a town without enough of
    them gets a NULL resale value and a report line saying so.
    """
    c = cfg["comps"]
    a = cfg["acquisition"]
    n = len(towns)
    now = date.today().year

    sale_year, price = d["ls_year"], d["ls_price"]
    res, lot = d["res_area"], d["lot_acres_used"]
    with np.errstate(invalid="ignore"):
        ppsf = price / res
        # min_price is not cosmetic: LS_PRICE carries thousands of $1 and $100
        # family transfers, and left in they invert the fitted price trend.
        recent = (np.isfinite(sale_year) & (now - sale_year <= c["lookback_years"])
                  & (price >= c["min_price"]))
        is_comp = (recent & (d["year_built"] >= c["min_year_built"])
                   & (sale_year - d["year_built"] >= -1)
                   & (sale_year - d["year_built"] <= c["new_build_window_years"])
                   & (price >= c["min_price"]) & np.isfinite(res) & (res > 0))

    trended = np.full(n, np.nan)
    town_ppsf = np.full(n, np.nan)
    town_price = np.full(n, np.nan)
    town_sqft = np.full(n, np.nan)
    market_ceiling = np.full(n, np.nan)
    observed_far_town = np.full(n, np.nan)
    acq_ratio = np.full(n, np.nan)
    land_multiple = np.full(n, np.nan)
    report = []

    lot_sqft = lot * SQ_FT_PER_ACRE
    with np.errstate(invalid="ignore"):
        comp_far = np.where(is_comp & (lot_sqft > 0), res / lot_sqft, np.nan)

    # Regional appreciation is a regional phenomenon, and pooling gets ~30k
    # sales instead of a hundred. Weston's own slope fits at 9.3%/yr, which
    # doubles an eight-year-old sale; the pooled slope is the honest one.
    pooled = model.fit_price_trend(np.where(recent, sale_year, np.nan),
                                   np.where(recent, ppsf, np.nan), cfg)
    cap = c["max_trend_adjustment"]

    for t in np.unique(towns):
        m = towns == t
        town_trend = model.fit_price_trend(
            np.where(m & recent, sale_year, np.nan),
            np.where(m & recent, ppsf, np.nan), cfg)
        trend = pooled if c["trend_scope"] == "pooled" else town_trend
        with np.errstate(invalid="ignore"):
            # A comparable needing a 2x time adjustment is not a comparable,
            # whatever the trend line says.
            tr = price * np.clip(np.exp(trend * (now - sale_year)), 1 / cap, cap)
        trended[m] = tr[m]

        cm = m & is_comp
        ncomp = int(cm.sum())
        mape = model.loo_mape(np.where(cm, tr, np.nan),
                              np.where(cm, res, np.nan), cfg)
        enough = ncomp >= c["min_comps_town"]
        if ncomp:
            town_ppsf[m] = np.nanmedian((tr / res)[cm])
            town_price[m] = np.nanmedian(tr[cm])
            town_sqft[m] = np.nanmedian(res[cm].astype(float))
            market_ceiling[m] = np.nanquantile(
                res[cm].astype(float),
                cfg["buildable"]["market_ceiling_percentile"])
            if int(np.isfinite(comp_far[cm]).sum()) >= c["min_comps_town"]:
                observed_far_town[m] = np.nanquantile(
                    comp_far[cm][np.isfinite(comp_far[cm])],
                    cfg["buildable"]["observed_far_percentile"])

        # Assessed value is not market value. Measure the ratio rather than
        # assume it: these five towns land near 1.04.
        with np.errstate(invalid="ignore"):
            rm = (m & np.isfinite(sale_year)
                  & (now - sale_year <= a["ratio_lookback_years"])
                  & (price >= c["min_price"]) & (d["total_val"] > 0))
            if int(rm.sum()) >= a["ratio_min_sales"]:
                acq_ratio[m] = np.nanmedian((price / d["total_val"])[rm])
            else:
                acq_ratio[m] = a["ratio_fallback"]
            # Where the assessor suppresses the structure value, TOTAL_VAL is
            # depressed with it and the multiple above reads as a bargain. Those
            # parcels price off LAND_VAL instead, grossed up by what houses with
            # a reported structure actually sell for relative to their land.
            lm = rm & (d["bldg_val_missing"] == 0) & (d["land_val"] > 0)
            if int(lm.sum()) >= a["land_multiple_min_sales"]:
                land_multiple[m] = np.nanmedian(
                    price[lm] / d["land_val"][lm])
            else:
                land_multiple[m] = a["land_multiple_fallback"]
        if not enough:
            # No fallback that invents a level. Too few recorded new-build
            # sales means no resale value for that town, and the report says so.
            town_ppsf[m] = town_price[m] = town_sqft[m] = np.nan
        report.append((str(t), ncomp, town_trend, mape, float(town_ppsf[m][0]),
                       float(observed_far_town[m][0]), float(acq_ratio[m][0]),
                       enough))

    if not quiet:
        log(f"  comparable new-construction sales, per town  "
            f"(price trend: {c['trend_scope']} {pooled:.1%}/yr, "
            f"each comparable adjusted at most {cap:.2f}x, "
            f"resale values then cut {1 - c['conservatism']:.0%})")
        log(f"    {'town':<12}{'comps':>7}{'own trend':>11}{'LOO error':>11}"
            f"{'$/sqft':>9}{'obs FAR':>9}{'assess->sale':>14}")
        for t, ncomp, town_trend, mape, pps, far, ratio, enough in report:
            flag = "" if enough else "   (too few comps: resale value NULL)"
            err = "     n/a" if not np.isfinite(mape) else f"{mape:>10.1%}"
            log(f"    {t:<12}{ncomp:>7}{town_trend:>10.1%}{err:>11}"
                f"{pps:>9.0f}{far:>9.2f}{ratio:>14.3f}{flag}")

    return {
        "is_comp": is_comp, "trended_price": trended, "town_ppsf": town_ppsf,
        "town_price": town_price, "town_sqft": town_sqft,
        "market_ceiling": market_ceiling, "observed_far_town": observed_far_town,
        "comp_far": comp_far, "acq_ratio": acq_ratio, "land_multiple": land_multiple,
        "report": report,
    }


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

SELECT_COMP = """
  SELECT row_id, town, x26986, y26986, res_area, bld_area, existing_sqft,
         year_built, lot_acres, lot_acres_used, gis_acres, land_val, bldg_val,
         total_val, ls_price, ls_year, land_ratio, impr_psf, far,
         years_since_sale, min_lot_acres, min_frontage_ft, max_far,
         max_lot_coverage, zoning, zoning_assignment_authority,
         absentee, estate, care_of,
         bldg_val_missing, n_records_on_parcel, lot_compactness, lot_width_ft,
         lot_depth_ft, fp_area_sqft, fp_min_setback_ft,
         flag_wetland_m, flag_sewer, flag_flood, flag_acec, flag_historic,
         flag_historic_area, flag_historic_designations,
         listing_status, list_price, original_list_price, days_on_market
  FROM comp ORDER BY row_id
"""


def _arrays(con) -> dict[str, np.ndarray]:
    """Fetch `comp` as float/object arrays with NULL as NaN.

    DuckDB hands back masked arrays for nullable columns; filling with NaN here
    once means nothing downstream can accidentally read a fill value as data.
    """
    cur = con.execute(SELECT_COMP)
    d = cur.fetchnumpy()
    out = {}
    for k, v in d.items():
        if k in ("town", "zoning", "zoning_assignment_authority",
                 "flag_flood", "flag_acec", "flag_historic",
                 "flag_historic_area", "flag_historic_designations",
                 "listing_status"):
            out[k] = np.asarray(v, dtype=object)
        else:
            out[k] = np.ma.filled(np.ma.asarray(v).astype("float64"), np.nan)
    return out


def _text_has(arr: np.ndarray, *needles: str) -> np.ndarray:
    """Case-insensitive substring test over an object array, NULL-safe."""
    up = np.array([("" if v is None else str(v)).upper() for v in arr])
    out = np.zeros(len(up), dtype=bool)
    for nd in needles:
        out |= np.char.find(up, nd.upper()) >= 0
    return out


def score_parcels(con, cfg, cov, out: Path, name="parcels", quiet=False,
                  flags_enabled=True, weights: dict | None = None):
    """Assemble every input, run model.py, write the parquet."""
    if weights:
        cfg = {**cfg, **{k: {**cfg.get(k, {}), **v} for k, v in weights.items()}}
    d = _arrays(con)
    n = len(d["row_id"])
    towns = d["town"]
    quiet or log(f"{n} parcels across {len(np.unique(towns))} towns")

    xy = np.column_stack([d["x26986"], d["y26986"]])
    idx = model.knn_index(towns, xy, min(cfg["neighborhood"]["k"], n))

    # ---- boolean constraint views the model expects ----------------------
    d["flag_flood_sfha"] = np.array([v is not None and str(v) != ""
                                     for v in d["flag_flood"]])
    d["flag_acec_present"] = np.array([v is not None and str(v) != ""
                                       for v in d["flag_acec"]])
    d["flag_historic_inventory"] = np.array([v is not None and str(v) != ""
                                             for v in d["flag_historic"]])
    d["flag_historic_area_present"] = np.array([v is not None and str(v) != ""
                                                for v in d["flag_historic_area"]])
    desig = d["flag_historic_designations"]
    # LHD = local historic district (a demolition certificate, i.e. a real bar).
    # PR  = a recorded preservation restriction, which runs with the land.
    local_hd = _text_has(desig, "LHD")
    restriction = (_text_has(desig, "PR")
                   | _text_has(d["flag_historic"], "PRESERVATION RESTRICTION"))
    d["flag_historic_local_district"] = d["flag_historic_area_present"] & local_hd
    d["flag_historic_restriction"] = restriction
    d["flag_historic_district"] = (d["flag_historic_area_present"]
                                   & ~local_hd & ~restriction)

    # ---- comparable sales, envelope, resale value ------------------------
    cs = comparable_sales(d, cfg, towns, xy, quiet=quiet)
    c = cfg["comps"]
    cidx, cdist = model.comp_neighbors(xy, cs["is_comp"], towns,
                                       c["min_comps_neighborhood"],
                                       c["neighborhood_radius_m"])
    with np.errstate(invalid="ignore"):
        comp_ppsf_row = np.where(np.isfinite(d["res_area"]) & (d["res_area"] > 0),
                                 cs["trended_price"] / d["res_area"], np.nan)
        nb_ppsf, nb_cnt = model._masked_median(comp_ppsf_row, cidx, cdist)
        nb_price, _ = model._masked_median(cs["trended_price"], cidx, cdist)
        nb_sqft, _ = model._masked_median(d["res_area"].astype(float),
                                          cidx, cdist)
        nb_far, nb_far_cnt = model._masked_median(cs["comp_far"], cidx, cdist)

    # Local comparables where there are enough of them in range, the town's
    # otherwise. Which one was used is what separates an authoritative resale
    # value from an estimated one.
    enough_local = nb_cnt >= c["min_comps_neighborhood"]
    comp_price = np.where(enough_local, nb_price, cs["town_price"])
    comp_sqft = np.where(enough_local, nb_sqft, cs["town_sqft"])
    comp_ppsf = np.where(enough_local, nb_ppsf, cs["town_ppsf"])
    observed_far = np.where(
        (nb_far_cnt >= cfg["buildable"]["min_observed_neighborhood"])
        & np.isfinite(nb_far), nb_far, cs["observed_far_town"])

    envelope_raw, buildable_sqft, env_src = model.buildable_envelope(
        {"lot_acres": d["lot_acres_used"], "max_far": d["max_far"],
         "max_lot_coverage": d["max_lot_coverage"]},
        cfg, observed_far, cs["market_ceiling"])

    arv = model.resale_value(buildable_sqft, comp_price, comp_sqft, comp_ppsf,
                             cfg) * c["conservatism"]

    # ---- acquisition price ----------------------------------------------
    a = cfg["acquisition"]
    with np.errstate(invalid="ignore"):
        suppressed = d["bldg_val_missing"] == 1
        basis = np.where(suppressed & (d["land_val"] > 0),
                         d["land_val"] * cs["land_multiple"],
                         d["total_val"] * cs["acq_ratio"])
        est_acq = np.where(basis > 0, basis * a["off_market_premium"], np.nan)
        listed = np.isfinite(d["list_price"]) & (d["list_price"] > 0)
        acq = np.where(listed, d["list_price"] * a["listed_price_premium"], est_acq)

    # ---- feasibility -----------------------------------------------------
    # existing_sqft (gross, BLD_AREA) drives demolition cost; living area drives
    # the downzoning comparison, because the envelope is inferred from the
    # living area of new builds.
    feas, feas_notes, gate_masks = model.feasibility(
        {**d, "lot_acres": d["lot_acres_used"],
         "existing_living_sqft": d["res_area"]}, cfg, envelope_raw,
        buildable_sqft)

    # ---- economics -------------------------------------------------------
    pf = model.pro_forma(d, cfg, arv, buildable_sqft, acq)
    sens = model.sensitivity(d, cfg, arv, buildable_sqft, acq)
    with np.errstate(invalid="ignore"):
        subdivision_lots = np.floor(d["lot_acres_used"] / d["min_lot_acres"])
    econ = model.economics_score(pf["margin"], sens["margin_if_all"],
                                 subdivision_lots, cfg)

    # ---- obsolescence and acquisition likelihood -------------------------
    res = d["res_area"]
    with np.errstate(invalid="ignore"):
        nbhd_med_res_area = np.nanmedian(res[idx], axis=1)
        raw_vals = {
            "land_ratio": d["land_ratio"],
            "impr_psf": d["impr_psf"],
            "age_fit": model.age_fit(d["year_built"], cfg),
            "far": d["far"],
            "area_vs_nbhd": res / nbhd_med_res_area,
            "years_since_sale": d["years_since_sale"],
        }
    floor = cfg["completeness"]["min_town_coverage"]
    for comp_name, arr in raw_vals.items():
        src = SOURCE_FIELD[comp_name]
        for t in np.unique(towns):
            m = towns == t
            if cov.get((t, src), 1.0) < floor and not np.isnan(arr[m]).all():
                quiet or log(f"  dropping {comp_name} for {t}: "
                             f"{src} coverage {cov[(t, src)]:.1%} < {floor:.0%}")
                arr[m] = np.nan

    pct_town = {k: model.pct_rank(raw_vals[k], towns, dr)
                for k, dr in OBSOLESCENCE.items() if k in raw_vals}
    pct_nbr = {k: model.nbr_pct_rank(raw_vals[k], idx, dr)
               for k, dr in OBSOLESCENCE.items() if k in raw_vals}
    pct_town["years_since_sale"] = model.pct_rank(
        raw_vals["years_since_sale"], towns, "hi")
    pct_nbr["years_since_sale"] = model.nbr_pct_rank(
        raw_vals["years_since_sale"], idx, "hi")
    scoring = (pct_town if cfg["run"]["percentile_scope"] == "town" else pct_nbr)

    obs_parts = {k: scoring[k] for k in OBSOLESCENCE}
    obs_weights = {k: cfg["obsolescence"]["age" if k == "age_fit" else k]
                   for k in OBSOLESCENCE}
    obsolescence, obs_cov = model.weighted_subscore(obs_parts, obs_weights)

    al = cfg["acquisition_likelihood"]
    on_market = _text_has(d["listing_status"], "ACTIVE", "COMING SOON",
                          "PENDING", "CONTINGENT")
    dead_listing = _text_has(d["listing_status"], "WITHDRAWN", "EXPIRED",
                             "CANCELED", "CANCELLED")
    has_status = np.array([v is not None and str(v) != ""
                           for v in d["listing_status"]])
    with np.errstate(invalid="ignore"):
        cut = np.where(np.isfinite(d["original_list_price"])
                       & np.isfinite(d["list_price"]),
                       (d["list_price"] < d["original_list_price"]).astype(float),
                       np.nan)
        acq_parts = {
            "years_since_sale": scoring["years_since_sale"],
            "absentee": d["absentee"],
            "estate": d["estate"],
            "care_of": d["care_of"],
            "listed": np.where(has_status, on_market.astype(float), np.nan),
            "days_on_market": np.clip(d["days_on_market"] / al["stale_days"],
                                      0.0, 1.0),
            "price_cut": cut,
            "withdrawn_or_expired": np.where(has_status,
                                             dead_listing.astype(float), np.nan),
        }
    for k in list(acq_parts):
        src = SOURCE_FIELD[k]
        for t in np.unique(towns):
            m = towns == t
            if (cov.get((t, src), 1.0) < floor
                    and not np.isnan(acq_parts[k][m]).all()):
                acq_parts[k][m] = np.nan
    acq_like, acq_cov = model.weighted_subscore(
        acq_parts, {k: al[k] for k in ACQUISITION})

    # ---- provenance and confidence ---------------------------------------
    with np.errstate(invalid="ignore"):
        lot_agree = (np.isfinite(d["lot_acres"]) & np.isfinite(d["gis_acres"])
                     & (np.abs(d["lot_acres"] - d["gis_acres"])
                        <= 0.25 * np.fmax(d["lot_acres"], d["gis_acres"])))
    official_zone = np.array([
        v is not None and str(v) for v in d["zoning_assignment_authority"]
    ], dtype=bool)
    known_zone = np.array([
        v is not None and str(v) for v in d["zoning"]
    ], dtype=bool)
    prov = {
        "zoning": np.where(np.isfinite(d["min_lot_acres"]) & official_zone,
                           AUTHORITATIVE,
                           np.where(known_zone, ESTIMATED, MISSING)),
        # "authoritative" here means the resale value rests on enough RECORDED
        # local new-build sales, not that the number itself is a fact.
        "resale_value": np.where(np.isfinite(arv) & enough_local, AUTHORITATIVE,
                                 np.where(np.isfinite(arv), ESTIMATED, MISSING)),
        "lot_area": np.where(lot_agree, AUTHORITATIVE,
                             np.where(np.isfinite(d["lot_acres_used"]),
                                      ESTIMATED, MISSING)),
        "structure_value": np.where((d["bldg_val_missing"] == 0)
                                    & (d["bldg_val"] > 0),
                                    AUTHORITATIVE, MISSING),
        "living_area": np.where(np.isfinite(d["bld_area"]), AUTHORITATIVE,
                                np.where(np.isfinite(d["res_area"]),
                                         ESTIMATED, MISSING)),
        "acquisition_price": np.where(listed, AUTHORITATIVE,
                                      np.where(np.isfinite(est_acq),
                                               ESTIMATED, MISSING)),
        "constraints": (np.where(np.isfinite(d["flag_sewer"]), AUTHORITATIVE,
                                 ESTIMATED) if flags_enabled
                        else np.full(n, MISSING)),
        "buildable_envelope": np.where(np.isfinite(buildable_sqft), env_src,
                                       MISSING),
    }
    conf = model.confidence_score(prov, cfg)

    # ---- final ranking ---------------------------------------------------
    score = model.combine(feas, econ, conf, acq_like, obsolescence, cfg)

    arrays = {
        "row_id": d["row_id"].astype("int64"),
        "score": score,                       # the tile layer joins on this name
        "rebuild_opportunity_score": score,
        "rebuild_feasibility_score": feas,
        "project_economics_score": econ,
        "acquisition_likelihood_score": acq_like,
        "structure_obsolescence_score": obsolescence,
        "data_confidence_score": conf,
        "feasibility_notes": feas_notes,
        "obsolescence_coverage": obs_cov,
        "acquisition_coverage": acq_cov,
        "nbhd_med_res_area": nbhd_med_res_area,
        "buildable_sqft": buildable_sqft,
        "envelope_raw_sqft": envelope_raw,
        "observed_far": observed_far,
        "comp_count_local": nb_cnt.astype("float64"),
        "comp_ppsf_local": nb_ppsf,
        "comp_ppsf_town": cs["town_ppsf"],
        "comp_price_used": comp_price,
        "comp_sqft_used": comp_sqft,
        "acq_ratio_town": cs["acq_ratio"],
        "est_acq_price": est_acq,
        "subdivision_lots": subdivision_lots,
        "is_newbuild_comp": cs["is_comp"],
    }
    arrays.update(pf)
    arrays.update(sens)
    for k, v in prov.items():
        arrays[f"prov_{k}"] = v.astype("int8")
    for k in OBSOLESCENCE:
        arrays[f"val_{k}"] = raw_vals[k]
        arrays[f"pct_{k}"] = pct_town[k]
        arrays[f"nbr_{k}"] = pct_nbr[k]
    arrays["val_years_since_sale"] = raw_vals["years_since_sale"]
    arrays["pct_years_since_sale"] = pct_town["years_since_sale"]
    arrays["nbr_years_since_sale"] = pct_nbr["years_since_sale"]
    for g in model.GATES:
        arrays[f"gate_{g}"] = np.asarray(gate_masks[g], dtype=bool)
    con.register("scores", arrays)

    parquet = out / f"{name}.parquet"
    con.execute(f"""
    COPY (
      SELECT c.town, c.town_id, c.release, c.fy, c.loc_id, c.prop_id,
             c.site_addr, c.city, c.owner1, c.own_addr, c.own_city, c.own_state,
             c.own_co, c.use_code, c.use_desc, c.zoning, c.zoning_authority,
             c.zoning_assignment_authority,
             c.style, c.stories, c.num_rooms, c.units,
             c.land_val, c.bldg_val, c.other_val, c.total_val,
             c.year_built, c.res_area, c.bld_area, c.existing_sqft,
             c.lot_acres, c.gis_acres, c.lot_acres_used,
             c.lot_compactness, c.lot_width_ft, c.lot_depth_ft,
             c.fp_area_sqft, c.fp_count, c.fp_min_setback_ft,
             c.ls_date, c.ls_price, c.min_lot_acres, c.min_frontage_ft,
             c.max_far, c.max_lot_coverage,
             c.listing_status, c.list_price, c.original_list_price,
             c.days_on_market,
             c.n_records_on_parcel, c.absentee, c.estate, c.care_of,
             c.bldg_val_missing,
             c.flag_wetland_m, c.flag_sewer, c.flag_flood, c.flag_acec,
             c.flag_historic, c.flag_historic_area,
             c.flag_historic_designations,
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
    quiet or score_report(con, parquet, cfg)
    return parquet


def score_report(con, parquet: Path, cfg):
    """What the model concluded, and the effective weight of every input.

    Printed on every build because a model whose behaviour you cannot see is a
    model you cannot argue with.
    """
    r = con.execute(f"""
      SELECT count(*) n,
             count(*) FILTER (WHERE rebuild_feasibility_score = 0) blocked,
             count(*) FILTER (WHERE arv IS NULL) no_arv,
             count(*) FILTER (WHERE margin > 0) profitable,
             count(*) FILTER (WHERE project_economics_score = 0) econ_zero,
             median(data_confidence_score) conf,
             median(margin) med_margin
      FROM read_parquet('{parquet}')""").fetchone()
    log(f"\n  scored {r[0]:,}  ·  feasibility-blocked {r[1]:,} "
        f"({r[1] / r[0]:.1%})  ·  no resale value {r[2]:,}  ·  "
        f"positive margin {r[3]:,} ({r[3] / r[0]:.1%})  ·  "
        f"median confidence {r[5]:.2f}  ·  median margin "
        f"{'n/a' if r[6] is None else f'{r[6]:.1%}'}")

    log("\n  effective weight of each input (share of total, as configured)")
    s = cfg["score"]
    log(f"    core is multiplicative: feasibility^{s['feasibility_exponent']} "
        f"x economics^{s['economics_exponent']} x "
        f"confidence^{s['confidence_exponent']}")
    log(f"    supporting term spans [1, {1 + s['support_strength']:.2f}] and is "
        f"{s['support_acquisition_likelihood']:.0%}/"
        f"{s['support_structure_obsolescence']:.0%} "
        f"acquisition-likelihood / obsolescence")
    for section in ("obsolescence", "acquisition_likelihood", "confidence"):
        keys = [k for k, v in cfg[section].items()
                if isinstance(v, (int, float)) and k not in
                ("stale_days", "estimated_credit", "floor")]
        tot = sum(cfg[section][k] for k in keys)
        parts = ", ".join(f"{k} {cfg[section][k] / tot:.0%}" for k in keys)
        log(f"    [{section}] {parts}")

    log("\n  per-town margin distribution and how many parcels clear the gates")
    cur = con.execute(f"""
      SELECT town, count(*) n,
             count(*) FILTER (WHERE rebuild_feasibility_score = 0) blocked,
             count(*) FILTER (WHERE arv IS NULL) no_arv,
             median(arv) arv, median(est_acq_price) acq,
             median(total_cost) all_in, median(margin) margin,
             count(*) FILTER (WHERE margin >= 0.20) strong,
             median(data_confidence_score) conf
      FROM read_parquet('{parquet}') GROUP BY 1 ORDER BY 1""")
    log(f"    {'town':<11}{'n':>7}{'blocked':>9}{'no ARV':>8}{'ARV':>12}"
        f"{'acq':>11}{'cost':>12}{'margin':>9}{'>=20%':>7}{'conf':>7}")
    for t, n, blk, na, av, ac, co, mg, st, cf in cur.fetchall():
        f = lambda v, w, k="": ("—".rjust(w) if v is None else
                                (f"{v:,.0f}".rjust(w) if k == "n"
                                 else f"{v:.1%}".rjust(w)))
        log(f"    {t:<11}{n:>7,}{blk:>9,}{na:>8,}{f(av, 12, 'n')}"
            f"{f(ac, 11, 'n')}{f(co, 12, 'n')}{f(mg, 9)}{st:>7,}{cf or 0:>7.2f}")

    # What construction cost per square foot the observed market would support,
    # given everything else. The median parcel is not a teardown candidate and
    # supports very little; the top decile is the number to argue about with a
    # builder. All-negative margins usually mean this assumption is wrong rather
    # than that the towns are unbuildable.
    implied = f"""(arv - (acq_price + buy_cost + demo_cost + carry_cost
                          + sale_cost) - {cfg['costs']['site_work']}
                   - {cfg['costs']['utility_connections']})
                  / (buildable_sqft * (1 + {cfg['costs']['design_permitting_pct']}
                     + {cfg['costs']['contingency_pct']}))"""
    imp = con.execute(f"""
      SELECT town, median({implied}), quantile_cont({implied}, 0.9)
      FROM read_parquet('{parquet}')
      WHERE arv IS NOT NULL AND buildable_sqft > 0 GROUP BY 1 ORDER BY 1""").fetchall()
    log(f"\n  construction cost per sqft the market supports "
        f"(configured: ${cfg['costs']['construction_per_sqft']:.0f})")
    log("    " + "   ".join(f"{t} median ${m:,.0f} / best decile ${p:,.0f}"
                            for t, m, p in imp if m is not None))

    log("\n  gates fired")
    for g in model.GATES:
        k = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet}') WHERE gate_{g}"
        ).fetchone()[0]
        if k:
            log(f"    {g:<26}{k:>7,}  x{cfg['feasibility'][g]:.2f}")

    log("\n  data provenance (share of parcels)")
    for k in model.CONFIDENCE_INPUTS:
        row = con.execute(f"""
          SELECT avg((prov_{k} = 2)::INT), avg((prov_{k} = 1)::INT),
                 avg((prov_{k} = 0)::INT) FROM read_parquet('{parquet}')""").fetchone()
        log(f"    {k:<20} authoritative {row[0]:>6.1%}   estimated {row[1]:>6.1%}"
            f"   missing {row[2]:>6.1%}")


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
        name="parcels", tiles=True, quiet=False, flags=True,
        weights: dict | None = None):
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

    flags_on = flags and bool(cfg.get("flags", {}).get("enabled"))
    if flags:
        spatial_flags(con, cfg)
    else:
        con.execute(f"CREATE OR REPLACE TABLE parcel_flags ({FLAG_COLUMNS})")
    inputs = optional_inputs(cfg["_dir"], quiet=quiet)
    build_components(con, cfg, inputs, quiet=quiet)
    cov = completeness(con, cfg["_out"], name=name)
    parquet = score_parcels(con, cfg, cov, cfg["_out"], name=name, quiet=quiet,
                            flags_enabled=flags_on, weights=weights)
    if tiles:
        build_tiles(con, cfg, cfg["_out"], parquet)
    return con, parquet


def main(cfg_path="config.toml"):
    con, _ = run(load_config(cfg_path))
    con.close()


def _selfcheck():
    """Model selfcheck plus the build-side glue the model cannot see."""
    model.selfcheck()

    # Text matching over object arrays has to survive NULLs, which is exactly
    # where a naive .upper() blows up.
    a = np.array(["Active", None, "WITHDRAWN", ""], dtype=object)
    assert list(_text_has(a, "ACTIVE")) == [True, False, False, False]
    assert list(_text_has(a, "withdrawn", "expired")) == [False, False, True, False]

    # Lot shape maths, against shapes whose answer is known.
    side = 100.0
    square = 4 * math.pi * side ** 2 / (4 * side) ** 2
    assert abs(square - 0.7853981) < 1e-6, square
    rect = 4 * math.pi * (400 * 100) / (2 * (400 + 100)) ** 2
    assert 0.49 < rect < 0.51, rect             # a 4:1 lot lands near 0.50

    cfg = model._cfg() | {
        "run": {"percentile_scope": "town"}, "neighborhood": {"k": 3},
        "completeness": {"min_town_coverage": 0.5},
        "obsolescence": {"land_ratio": 0.34, "impr_psf": 0.12,
                         "area_vs_nbhd": 0.24, "far": 0.16, "age": 0.14},
        "acquisition_likelihood": {k: 0.125 for k in ACQUISITION} | {"stale_days": 120},
        "acquisition": {"ratio_lookback_years": 3, "ratio_min_sales": 2,
                        "ratio_fallback": 1.05, "off_market_premium": 1.05,
                        "listed_price_premium": 1.0,
                        "land_multiple_min_sales": 2, "land_multiple_fallback": 1.9},
        "comps": {"new_build_window_years": 3, "min_year_built": 2012,
                  "lookback_years": 10, "min_price": 400000,
                  "min_comps_town": 3, "min_comps_neighborhood": 2,
                  "marginal_sqft_share": 0.60, "trend_scope": "pooled",
                  "max_trend_adjustment": 1.35, "conservatism": 0.95,
                  "max_implied_ppsf_share": 1.25,
                  "min_implied_ppsf_share": 0.75,
                  "neighborhood_radius_m": 2000.0, "max_annual_trend": 0.12,
                  "min_annual_trend": -0.05, "market_ceiling_percentile": 0.95,
                  "max_neighborhood_adjustment": 1.35,
                  "min_neighborhood_adjustment": 0.70},
        "buildable": {"observed_far_percentile": 0.75,
                      "min_observed_neighborhood": 2, "min_observed_town": 3,
                      "min_viable_sqft": 1800.0, "max_home_sqft": 7500.0,
                      "market_ceiling_percentile": 0.95},
    }
    cfg["comps"]["market_ceiling_percentile"] = 0.95

    # A hand-built five-parcel town: four recent new-build sales to serve as
    # comparables, plus one 1955 candidate. The candidate must come out ranked,
    # priced and explained without any external dataset.
    con = duckdb.connect()
    con.execute("""
      CREATE TABLE comp AS SELECT * FROM (VALUES
        (1,'T',0.0,0.0,  4000,4000,4000.0, 2019, 0.30,0.30,0.30, 900000,1600000,2500000, 2500000, 2019.0, 0.36,400.0,0.31, 6.0, NULL,NULL,NULL,NULL,'R',NULL, false,false,false, false, 1, 0.8, 120.0,150.0, NULL,NULL, NULL,true,NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL),
        (2,'T',100.0,0.0, 4200,4200,4200.0, 2020, 0.32,0.32,0.32, 950000,1700000,2650000, 2700000, 2021.0, 0.36,405.0,0.30, 4.0, NULL,NULL,NULL,NULL,'R',NULL, false,false,false, false, 1, 0.8, 120.0,150.0, NULL,NULL, NULL,true,NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL),
        (3,'T',200.0,0.0, 3900,3900,3900.0, 2021, 0.28,0.28,0.28, 900000,1600000,2500000, 2600000, 2022.0, 0.36,410.0,0.32, 3.0, NULL,NULL,NULL,NULL,'R',NULL, false,false,false, false, 1, 0.8, 120.0,150.0, NULL,NULL, NULL,true,NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL),
        (4,'T',300.0,0.0, 4100,4100,4100.0, 2022, 0.31,0.31,0.31, 950000,1750000,2700000, 2800000, 2023.0, 0.35,420.0,0.30, 2.0, NULL,NULL,NULL,NULL,'R',NULL, false,false,false, false, 1, 0.8, 120.0,150.0, NULL,NULL, NULL,true,NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL),
        (5,'T',150.0,50.0, 1600,1600,1600.0, 1955, 0.30,0.30,0.30, 900000,150000,1050000, 420000, 1994.0, 0.857,93.75,0.12, 31.0, NULL,NULL,NULL,NULL,'R',NULL, true,false,false, false, 1, 0.8, 120.0,150.0, NULL,NULL, NULL,true,NULL,NULL,NULL,NULL,NULL, NULL,NULL,NULL,NULL)
      ) AS t(row_id,town,x26986,y26986,res_area,bld_area,existing_sqft,
             year_built,lot_acres,lot_acres_used,gis_acres,land_val,bldg_val,
             total_val,ls_price,ls_year,land_ratio,impr_psf,far,
             years_since_sale,min_lot_acres,min_frontage_ft,max_far,
             max_lot_coverage,zoning,zoning_assignment_authority,
             absentee,estate,care_of,bldg_val_missing,
             n_records_on_parcel,lot_compactness,lot_width_ft,lot_depth_ft,
             fp_area_sqft,fp_min_setback_ft,flag_wetland_m,flag_sewer,
             flag_flood,flag_acec,flag_historic,flag_historic_area,
             flag_historic_designations,listing_status,list_price,
             original_list_price,days_on_market)
    """)
    d = _arrays(con)
    assert np.isnan(d["min_lot_acres"]).all()      # NULL never becomes a number
    cs = comparable_sales(d, cfg, d["town"], None, quiet=True)
    assert cs["is_comp"].sum() == 4, cs["is_comp"]
    assert not cs["is_comp"][4]                    # a 1994 sale is not a new build
    assert np.isfinite(cs["town_price"]).all() and np.isfinite(cs["town_ppsf"]).all()
    # And the resale value of the 1955 candidate is built from the four new
    # builds around it, not from its own $420k 1994 sale.
    arv = model.resale_value(np.array([3800.0]), cs["town_price"][4:5],
                             cs["town_sqft"][4:5], cs["town_ppsf"][4:5], cfg)
    assert 2_000_000 < arv[0] < 3_500_000, arv
    assert 1.0 < cs["acq_ratio"][0] < 1.2, cs["acq_ratio"]

    con.close()
    print("build selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        main(*[a for a in sys.argv[1:] if not a.startswith("-")])

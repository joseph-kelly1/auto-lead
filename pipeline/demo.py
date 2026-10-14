#!/usr/bin/env python3
"""Build the public demo from invented inputs, without reading source records.

Run from pipeline/: uv run demo.py
Requires tippecanoe. Schema matches the production artifact contract.
"""

import json
import hashlib
import math
import shutil
import subprocess
import tempfile
import tomllib
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

import model

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT.parent / "web" / "public"
# Town centres provide map context only. All parcel shapes and attributes are
# invented and do not follow assessor parcel boundaries or recorded addresses.
TOWNS = [
    ("Brookline", -71.12, 42.33),
    ("Newton", -71.21, 42.34),
    ("Wayland", -71.36, 42.36),
    ("Wellesley", -71.29, 42.30),
    ("Weston", -71.30, 42.37),
]
PER_TOWN = 24


def build_demo():
    if not shutil.which("tippecanoe"):
        raise SystemExit("tippecanoe is required: brew install tippecanoe")
    schema = json.loads((ROOT / "fixtures" / "demo-schema.json").read_text())
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    n = len(TOWNS) * PER_TOWN
    index = np.arange(n)
    existing = 1500.0 + (index % 9) * 140
    year = 1945.0 + index % 45
    sewer = (index % 4 != 0).astype(float)
    sqft = 3200.0 + (index % 6) * 250
    acq = 750_000.0 + (index % 8) * 80_000
    arv = 2_650_000.0 + (index % 7) * 160_000
    inputs = {"existing_sqft": existing, "year_built": year, "flag_sewer": sewer}
    pro_forma = model.pro_forma(inputs, cfg, arv, sqft, acq)
    sensitivities = model.sensitivity(inputs, cfg, arv, sqft, acq)
    economics = model.economics_score(
        pro_forma["margin"], sensitivities["margin_if_all"], np.ones(n), cfg,
    )
    feasibility = np.where(index % 11 == 0, 0.0, np.where(index % 7 == 0, 0.55, 1.0))
    provenance = {
        key: np.full(n, model.ESTIMATED if key == "acquisition_price" else model.AUTHORITATIVE)
        for key in model.CONFIDENCE_INPUTS
    }
    confidence = model.confidence_score(provenance, cfg)
    acquisition = 0.25 + (index % 5) * 0.12
    obsolescence = 0.4 + (index % 6) * 0.1
    scores = model.combine(feasibility, economics, confidence, acquisition, obsolescence, cfg)
    rows, features = [], []
    for i in range(n):
        town, centre_lon, centre_lat = TOWNS[i // PER_TOWN]
        cell = i % PER_TOWN
        lon = centre_lon + (cell % 6 - 2.5) * 0.001
        lat = centre_lat + (cell // 6 - 1.5) * 0.001
        loc_id = f"demo-{i + 1:03d}"
        row = {key: False if kind == "BOOLEAN" else None for key, kind in schema}
        row.update({
            "town": town, "town_id": i // PER_TOWN + 1, "city": town,
            "release": "synthetic-demo", "fy": 2026, "loc_id": loc_id,
            "prop_id": f"demo-property-{i + 1:03d}", "site_addr": f"Demo parcel {i + 1:03d}",
            "use_code": "101", "use_desc": "Fictional single-family parcel",
            "zoning": "DEMO", "zoning_authority": "Synthetic scenario",
            "zoning_assignment_authority": "Synthetic scenario", "style": "Demo house",
            "stories": "2", "num_rooms": 7, "units": 1, "other_val": 0,
            "land_val": int(acq[i] * 0.8), "bldg_val": int(acq[i] * 0.2),
            "total_val": int(acq[i]), "year_built": int(year[i]),
            "res_area": int(existing[i]), "bld_area": int(existing[i] * 1.4),
            "existing_sqft": float(existing[i]), "lot_acres": 0.4, "gis_acres": 0.4,
            "lot_acres_used": 0.4, "lot_compactness": math.pi / 4,
            "lot_width_ft": 120.0, "lot_depth_ft": 145.2,
            "ls_date": date(2010 + i % 10, 6, 1), "ls_price": int(acq[i] * 0.7),
            "min_lot_acres": 0.5 if feasibility[i] == 0 else 0.25,
            "min_frontage_ft": 100.0, "max_far": 0.35, "max_lot_coverage": 0.3,
            "n_records_on_parcel": 1, "flag_sewer": bool(sewer[i]),
            "flag_wetland_m": 20.0 if feasibility[i] == 0.55 else None,
            "score": float(scores[i]), "rebuild_opportunity_score": float(scores[i]),
            "rebuild_feasibility_score": float(feasibility[i]),
            "project_economics_score": float(economics[i]),
            "acquisition_likelihood_score": float(acquisition[i]),
            "structure_obsolescence_score": float(obsolescence[i]),
            "data_confidence_score": float(confidence[i]),
            "feasibility_notes": "nonconforming_lot" if feasibility[i] == 0 else
                "wetland_buffer" if feasibility[i] == 0.55 else "",
            "gate_nonconforming_lot": bool(feasibility[i] == 0),
            "gate_wetland_buffer": bool(feasibility[i] == 0.55),
            "obsolescence_coverage": 1.0, "acquisition_coverage": 0.25,
            "buildable_sqft": float(sqft[i]), "envelope_raw_sqft": float(sqft[i]),
            "observed_far": 0.25, "comp_count_local": 8.0,
            "comp_ppsf_local": float(arv[i] / sqft[i]),
            "comp_ppsf_town": float(arv[i] / sqft[i]),
            "comp_price_used": float(arv[i]), "comp_sqft_used": float(sqft[i]),
            "acq_ratio_town": 1.05, "est_acq_price": float(acq[i]),
            "subdivision_lots": 1.0, "val_land_ratio": 0.8,
            "lon": lon, "lat": lat,
        })
        for key, values in {**pro_forma, **sensitivities}.items():
            row[key] = float(values[i])
        for key, values in provenance.items():
            row[f"prov_{key}"] = int(values[i])
        rows.append([row[key] for key, _ in schema])
        dx, dy = 0.00035, 0.0003
        ring = [[lon-dx, lat-dy], [lon+dx, lat-dy], [lon+dx, lat+dy],
                [lon-dx, lat+dy], [lon-dx, lat-dy]]
        features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                         "properties": {"loc_id": loc_id, "score": float(scores[i])}})

    PUBLIC.mkdir(exist_ok=True)
    # Build both artifacts in a temporary directory before replacing the demo.
    with tempfile.TemporaryDirectory() as temp:
        staging = Path(temp)
        con = duckdb.connect()
        columns = ", ".join(f'"{key}" {kind}' for key, kind in schema)
        con.execute(f"CREATE TABLE demo ({columns})")
        con.executemany(f"INSERT INTO demo VALUES ({','.join('?' for _ in schema)})", rows)
        parquet = staging / "parcels.parquet"
        con.execute("COPY (SELECT * FROM demo ORDER BY score DESC) TO ? (FORMAT PARQUET)", [str(parquet)])
        con.close()
        geojson = staging / "parcels.geojsonl"
        geojson.write_text("\n".join(json.dumps(f) for f in features) + "\n")
        tiles = staging / "parcels.pmtiles"
        subprocess.run([
            "tippecanoe", "-q", "-o", tiles.name, "-l", "parcels",
            "-n", "Auto Lead synthetic demo", "-N", "Fictional parcels and financial inputs",
            "-Z", "10", "-z", "16", geojson.name,
        ], cwd=staging, check=True)
        for file in (parquet, tiles):
            shutil.copyfile(file, PUBLIC / file.name)
    hashes = {name: hashlib.sha256((PUBLIC / name).read_bytes()).hexdigest()
              for name in ("parcels.parquet", "parcels.pmtiles")}
    (PUBLIC / "dataset.json").write_text(
        json.dumps({"kind": "synthetic", "rows": n, "sha256": hashes}, indent=2) + "\n",
    )
    print(f"Built {n} fictional parcels in web/public. No source records read.")


if __name__ == "__main__":
    build_demo()

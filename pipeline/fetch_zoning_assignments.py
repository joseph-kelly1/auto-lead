#!/usr/bin/env python3
"""Refresh parcel zoning from official Newton and Wellesley GIS polygons.

The MassGIS assessor extract already carries district text for Wayland and
Weston. Wellesley leaves it empty, and Newton publishes a more authoritative
zoning layer, so these two towns are assigned by parcel-centroid containment.

Run after at least one parcel build exists::

    uv run fetch_zoning_assignments.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parent.parent
PARCELS = ROOT / "artifacts" / "parcels.parquet"
OUTPUT = Path(__file__).resolve().parent / "zoning_assignments.csv"

SOURCES = {
    "Newton": {
        "endpoint": "https://gisweb.newtonma.gov/server/rest/services/Data/MapServer/24/query",
        "field": "Zoning",
        "authority": "City of Newton official zoning GIS",
        "source_url": "https://gisweb.newtonma.gov/server/rest/services/Data/MapServer/24",
    },
    "Wellesley": {
        "endpoint": "https://services6.arcgis.com/f6G5SbcwuEVmR1CW/ArcGIS/rest/services/Open_Data_Layers/FeatureServer/26/query",
        "field": "ZoningCode",
        "authority": "Town of Wellesley official zoning GIS",
        "source_url": "https://services6.arcgis.com/f6G5SbcwuEVmR1CW/ArcGIS/rest/services/Open_Data_Layers/FeatureServer/26",
    },
}


def download_geojson(endpoint: str, field: str, destination: Path) -> None:
    features = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": f"OBJECTID,{field}",
            "returnGeometry": "true",
            "outSR": "4326",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": 2000,
            "f": "geojson",
        })
        request = urllib.request.Request(
            f"{endpoint}?{query}", headers={"User-Agent": "auto-lead-zoning/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response:
            parsed = json.loads(response.read())
        page = parsed.get("features", [])
        features.extend(page)
        if len(page) < 2000:
            break
        offset += len(page)
    if not features:
        raise RuntimeError(f"official zoning service returned no features: {endpoint}")
    destination.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def main() -> None:
    if not PARCELS.exists():
        raise SystemExit(f"run build.py first; missing {PARCELS}")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial")
    rows: list[tuple[str, str, str, str, str, str]] = []
    as_of = dt.date.today().isoformat()

    with tempfile.TemporaryDirectory(prefix="auto-lead-zoning-") as tmp:
        for town, source in SOURCES.items():
            path = Path(tmp) / f"{town.lower()}.geojson"
            download_geojson(source["endpoint"], source["field"], path)
            field = source["field"]
            assigned = con.execute(f"""
                SELECT p.loc_id, z.{field}::VARCHAR AS zoning
                FROM read_parquet(?) p
                JOIN ST_Read(?) z
                  ON ST_Intersects(z.geom, ST_Point(p.lon, p.lat))
                WHERE p.town = ? AND nullif(trim(z.{field}::VARCHAR), '') IS NOT NULL
                QUALIFY row_number() OVER (
                    PARTITION BY p.loc_id ORDER BY ST_Area(z.geom)
                ) = 1
                ORDER BY p.loc_id
            """, [str(PARCELS), str(path), town]).fetchall()
            total = con.execute(
                "SELECT count(DISTINCT loc_id) FROM read_parquet(?) WHERE town = ?",
                [str(PARCELS), town],
            ).fetchone()[0]
            coverage = len(assigned) / total if total else 0
            if coverage < 0.98:
                raise RuntimeError(
                    f"{town} zoning coverage only {len(assigned):,}/{total:,} ({coverage:.1%})")
            rows.extend((town, loc_id, zoning, source["authority"],
                         source["source_url"], as_of)
                        for loc_id, zoning in assigned)
            print(f"{town}: assigned {len(assigned):,}/{total:,} parcels ({coverage:.1%})")

    with OUTPUT.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["town", "loc_id", "zoning", "authority", "source_url", "as_of"])
        writer.writerows(sorted(rows))
    print(f"wrote {OUTPUT} ({len(rows):,} assignments)")


if __name__ == "__main__":
    main()

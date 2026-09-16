"""
Filter the GDOT/FHWA TMAS 2022 traffic volume CSV down to only the count
stations located inside I-285 (the Atlanta "Perimeter").

Station coordinates come from the FHWA/BTS TMAS Stations ArcGIS FeatureServer.
The I-285 loop geometry comes from OpenStreetMap (Overpass API, relation 240668,
tagged loc_name="The Perimeter"). A station is "inside" if its point falls
within the polygon formed by polygonizing I-285's mainline geometry.
"""

import csv
import json
import subprocess
import sys
import time
import urllib.parse

import pandas as pd
from shapely.geometry import LineString, Point, mapping
from shapely.ops import polygonize

INPUT_CSV = "Travel_Monitoring_Analysis_System_(TMAS)_Traffic_Volume_2022_20260910.csv"
OUTPUT_CSV = "Travel_Monitoring_Analysis_System_(TMAS)_Traffic_Volume_2022_InsidePerimeter.csv"
POLYGON_GEOJSON = "i285_polygon.geojson"
STATIONS_INSIDE_CSV = "stations_inside_perimeter.csv"

STATION_FEATURE_SERVICE = (
    "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/"
    "NTAD_Travel_Monitoring_Analysis_System_Stations/FeatureServer/0/query"
)

I285_RELATION_ID = 240668
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

CHUNK_SIZE = 500_000


def normalize_id(station_id: str) -> str:
    s = station_id.strip().strip('"')
    stripped = s.lstrip("0")
    return stripped if stripped else "0"


def curl_get_json(url: str, params: dict, timeout_s: int, user_agent: str = "cleanmydata-perimeter-filter/1.0"):
    """Fetch JSON via curl (uses the OS certificate store, unlike requests/certifi
    on this network, where TLS verification fails)."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    result = subprocess.run(
        ["curl", "-s", "-G", "-H", f"User-Agent: {user_agent}", "--max-time", str(timeout_s), full_url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl exited with code {result.returncode}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def fetch_ga_stations() -> list[dict]:
    stations = []
    offset = 0
    while True:
        params = {
            "where": "state='GA'",
            "outFields": "Station_Id,latitude,longitude,functional_class",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": 2000,
            "resultOffset": offset,
        }
        data = curl_get_json(STATION_FEATURE_SERVICE, params, timeout_s=60)
        feats = data.get("features", [])
        stations.extend(f["attributes"] for f in feats)
        if not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
    print(f"Fetched {len(stations)} GA station records from FHWA/BTS TMAS FeatureServer.")
    return stations


def fetch_i285_ways() -> list[dict]:
    query = f"[out:json][timeout:90];relation({I285_RELATION_ID});way(r);out geom;"
    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(4):
            try:
                data = curl_get_json(endpoint, {"data": query}, timeout_s=100)
                ways = [e for e in data.get("elements", []) if e["type"] == "way" and e.get("geometry")]
                if ways:
                    print(f"Fetched {len(ways)} I-285 way geometries from {endpoint}.")
                    return ways
                last_err = RuntimeError(f"no ways in response: {data}")
            except Exception as e:  # noqa: BLE001 - want to retry on any transient failure
                last_err = e
            print(f"  Overpass attempt {attempt + 1} on {endpoint} failed: {last_err}")
            time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"Could not fetch I-285 geometry from any Overpass endpoint: {last_err}")


def build_perimeter_polygon():
    ways = fetch_i285_ways()
    lines = [LineString([(n["lon"], n["lat"]) for n in w["geometry"]]) for w in ways]
    polys = list(polygonize(lines))
    if not polys:
        raise RuntimeError("Polygonizing I-285 geometry produced no closed polygon.")
    polygon = max(polys, key=lambda p: p.area)
    with open(POLYGON_GEOJSON, "w") as f:
        json.dump({"type": "Feature", "properties": {}, "geometry": mapping(polygon)}, f)
    print(f"I-285 perimeter polygon bounds: {polygon.bounds} (saved to {POLYGON_GEOJSON})")
    return polygon


def determine_inside_station_ids(csv_station_ids: set[str], polygon) -> tuple[set[str], list[dict]]:
    ga_stations = fetch_ga_stations()
    csv_norm_to_orig = {normalize_id(s): s for s in csv_station_ids}

    matched_norm_ids = set()
    inside_rows = []
    for attrs in ga_stations:
        raw_id = str(attrs["Station_Id"])
        norm_id = normalize_id(raw_id)
        if norm_id not in csv_norm_to_orig:
            continue
        matched_norm_ids.add(norm_id)
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lon is None:
            continue
        if polygon.contains(Point(lon, lat)):
            inside_rows.append(
                {
                    "station_id": csv_norm_to_orig[norm_id],
                    "latitude": lat,
                    "longitude": lon,
                    "functional_class": attrs.get("functional_class"),
                }
            )

    unmatched = sorted(csv_norm_to_orig[n] for n in (set(csv_norm_to_orig) - matched_norm_ids))
    if unmatched:
        print(
            f"WARNING: {len(unmatched)} of {len(csv_station_ids)} CSV station IDs have no "
            f"coordinate match in the FHWA/BTS station table and are excluded: {unmatched}"
        )

    with open(STATIONS_INSIDE_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["station_id", "latitude", "longitude", "functional_class"])
        writer.writeheader()
        writer.writerows(inside_rows)

    inside_ids = {row["station_id"] for row in inside_rows}
    print(f"{len(inside_ids)} of {len(csv_station_ids)} stations fall inside the Perimeter (saved to {STATIONS_INSIDE_CSV}).")
    return inside_ids, unmatched


def filter_big_csv(inside_ids: set[str]):
    total_rows = 0
    kept_rows = 0
    header_written = False

    for chunk in pd.read_csv(INPUT_CSV, dtype=str, chunksize=CHUNK_SIZE):
        total_rows += len(chunk)
        mask = chunk["Station ID"].isin(inside_ids)
        filtered = chunk[mask]
        kept_rows += len(filtered)
        filtered.to_csv(
            OUTPUT_CSV,
            mode="a" if header_written else "w",
            header=not header_written,
            index=False,
            quoting=csv.QUOTE_ALL,
        )
        header_written = True
        print(f"  processed {total_rows:,} rows so far, kept {kept_rows:,}", end="\r")

    print()
    print(f"Done. Kept {kept_rows:,} of {total_rows:,} rows -> {OUTPUT_CSV}")


def main():
    print("Reading unique station IDs from input CSV...")
    id_col = pd.read_csv(INPUT_CSV, dtype=str, usecols=["Station ID"])
    csv_station_ids = set(id_col["Station ID"].unique())
    print(f"Found {len(csv_station_ids)} unique station IDs in {INPUT_CSV}.")

    polygon = build_perimeter_polygon()
    inside_ids, _ = determine_inside_station_ids(csv_station_ids, polygon)

    if not inside_ids:
        print("No stations matched inside the Perimeter; aborting before filtering the big CSV.")
        sys.exit(1)

    filter_big_csv(inside_ids)


if __name__ == "__main__":
    main()

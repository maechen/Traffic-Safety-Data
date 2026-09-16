"""
Join GDOT point-in-time incidents with TADA annual exposure and TMAS station locations inside I-285.
  
GDOT = incident at an exact Date + Hour
TMAS = continuous-count station locations (perimeter roster)
TADA = annualized exposure (AADT, etc.) at the matched counter
"""

from pathlib import Path

import geopandas as gpd
import pandas as pd

RAW = Path("raw_data")
OUT = Path("Atlanta_Commuter_Final_Dataset.csv")
BUFFER_M = 250
ID_MATCH_MAX_M = 250  # reject ID hits that are geographically implausible
SPATIAL_MATCH_MAX_M = 250  # cap spatial fallback (same as crash buffer)
UTM_ATL = "EPSG:32616"  # WGS 84 / UTM zone 16N — meter units for Atlanta

TADA_EXPOSURE_COLS = [
    "TADA Station ID",
    "tada_join_id",
    "TADA Functional Class",
    "TADA Latitude",
    "TADA Longitude",
    "TADA Year",
    "TADA Station Type",
    "TADA Statistics Type",
    "AADT",
    "Single-Unit Truck AADT",
    "Combo-Unit Truck AADT",
    "% Peak SU Trucks",
    "% Peak CU Trucks",
    "K-Factor",
    "D-Factor",
    "Future AADT",
]


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.strip() for c in df.columns]
    return df


def normalize_station_id(series: pd.Series) -> pd.Series:
    """Key normalization: strip quotes/hyphens/leading zeros; guard NaN→'nan'."""
    return (
        series.astype(str)
        .str.strip()
        .str.replace('"', "", regex=False)
        .str.replace("-", "", regex=False)
        .str.lstrip("0")
        .replace("", "0")
        .replace("nan", "0")
        .replace("None", "0")
        .replace("<NA>", "0")
    )


def _attribute_link(
    remaining: gpd.GeoDataFrame,
    tada_roster: gpd.GeoDataFrame,
    left_on: str,
    right_on: str,
    method: str,
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """
    Deterministic ID merge, then proximity gate.
    Returns (accepted_matches, still_unmatched_tmas).
    """
    tada_attrs = pd.DataFrame(tada_roster.drop(columns=["geometry"]))
    merged = remaining.merge(tada_attrs, left_on=left_on, right_on=right_on, how="inner")
    if merged.empty:
        return merged, remaining

    # Distance in UTM meters between TMAS point and matched TADA station
    tada_geom = tada_roster.drop_duplicates("TADA Station ID").set_index("TADA Station ID")[
        "geometry"
    ]
    dist_vals = []
    for _, row in merged.iterrows():
        tg = tada_geom.get(row["TADA Station ID"])
        dist_vals.append(float("inf") if tg is None else row.geometry.distance(tg))
    merged = merged.copy()
    merged["TMAS_to_TADA_m"] = dist_vals

    # One TADA per TMAS: closest among ID hits
    merged = merged.sort_values("TMAS_to_TADA_m").drop_duplicates(subset=["_tmas_ix"], keep="first")
    ok = merged[merged["TMAS_to_TADA_m"] <= ID_MATCH_MAX_M].copy()
    ok["TMAS_TADA_match_method"] = method

    still = remaining[~remaining["_tmas_ix"].isin(ok["_tmas_ix"])].copy()
    return ok, still


def _spatial_fallback(remaining: gpd.GeoDataFrame, tada_roster: gpd.GeoDataFrame) -> pd.DataFrame:
    """
    Nearest TADA within SPATIAL_MATCH_MAX_M, preferring matching functional-class
    code (e.g. TMAS '1U' ↔ TADA '1U : ...') to reduce opposite-carriageway snaps.
    """
    tada_attrs = tada_roster.copy()
    tada_attrs["tada_fc_code"] = (
        tada_attrs["TADA Functional Class"]
        .astype(str)
        .str.split(":")
        .str[0]
        .str.strip()
    )

    rows = []
    sindex = tada_attrs.sindex
    for _, tmas_row in remaining.iterrows():
        buf = tmas_row.geometry.buffer(SPATIAL_MATCH_MAX_M)
        hit_idx = list(sindex.intersection(buf.bounds))
        if not hit_idx:
            continue
        cands = tada_attrs.iloc[hit_idx].copy()
        cands["TMAS_to_TADA_m"] = cands.geometry.distance(tmas_row.geometry)
        cands = cands[cands["TMAS_to_TADA_m"] <= SPATIAL_MATCH_MAX_M]
        if cands.empty:
            continue

        tmas_fc = str(tmas_row.get("TMAS Functional Class", "") or "").strip()
        same_fc = cands[cands["tada_fc_code"] == tmas_fc] if tmas_fc else cands.iloc[0:0]
        pick_pool = same_fc if len(same_fc) else cands
        pick = pick_pool.nsmallest(1, "TMAS_to_TADA_m").iloc[0]
        method = "spatial_same_fc" if len(same_fc) else "spatial_nearest"

        out = tmas_row.drop(labels=["geometry"]).to_dict()
        for col in TADA_EXPOSURE_COLS:
            out[col] = pick[col]
        out["TMAS_to_TADA_m"] = float(pick["TMAS_to_TADA_m"])
        out["TMAS_TADA_match_method"] = method
        rows.append(out)

    return pd.DataFrame(rows) if rows else remaining.drop(columns=["geometry"]).iloc[0:0]


def link_tmas_to_tada(gdf_tmas: gpd.GeoDataFrame, gdf_tada: gpd.GeoDataFrame) -> pd.DataFrame:
    """Attribute-first TMAS↔TADA crosswalk; spatial only for leftovers."""
    tada = gdf_tada.copy()
    tada["tada_suffix_id"] = normalize_station_id(
        tada["TADA Station ID"].astype(str).str.split("-").str[-1]
    )
    tada_roster = tada.drop_duplicates(subset=["TADA Station ID"], keep="first")
    keep_cols = TADA_EXPOSURE_COLS + ["geometry", "tada_suffix_id"]
    tada_roster = tada_roster[[c for c in keep_cols if c in tada_roster.columns]]

    remaining = gdf_tmas.copy().reset_index(drop=True)
    remaining["_tmas_ix"] = remaining.index

    matched: list[pd.DataFrame] = []

    ok, remaining = _attribute_link(
        remaining, tada_roster, left_on="join_id", right_on="tada_join_id", method="id_exact"
    )
    if len(ok):
        matched.append(ok.drop(columns=["geometry"], errors="ignore"))

    ok, remaining = _attribute_link(
        remaining, tada_roster, left_on="join_id", right_on="tada_suffix_id", method="id_suffix"
    )
    if len(ok):
        matched.append(ok.drop(columns=["geometry"], errors="ignore"))

    if len(remaining):
        spatial = _spatial_fallback(remaining, tada_roster)
        if len(spatial):
            matched.append(spatial)
            remaining = remaining[~remaining["_tmas_ix"].isin(spatial["_tmas_ix"])]

    if len(remaining):
        print(
            f"WARNING: {len(remaining)} TMAS stations unmatched within "
            f"{SPATIAL_MATCH_MAX_M} m: {remaining['TMAS Station ID'].tolist()}"
        )

    if not matched:
        return pd.DataFrame()

    crosswalk = pd.concat(matched, ignore_index=True)
    return crosswalk.drop(columns=["_tmas_ix", "tada_suffix_id"], errors="ignore")


def main() -> None:
    gdot = clean_columns(pd.read_csv(RAW / "GDOT_Collisions_Dataset.csv"))
    tada = clean_columns(pd.read_csv(RAW / "TADA_all_station_annualized_dataset.csv"))
    tmas = clean_columns(pd.read_csv(RAW / "tmas_stations_inside_perimeter.csv"))

    # Temporal Standardization
    gdot["Date"] = pd.to_datetime(gdot["Date"])
    # Accept HH:MM:SS or HH:MM
    gdot["Hour"] = pd.to_datetime(gdot["Time"], format="mixed").dt.hour
    gdot["Year"] = gdot["Date"].dt.year
    gdot["Month"] = gdot["Date"].dt.month
    gdot["Day"] = gdot["Date"].dt.day
    gdot["DayOfWeek"] = gdot["Date"].dt.day_name()

    tada["TADA Year"] = pd.to_numeric(tada["Year"], errors="coerce").astype("Int64")

    gdot = gdot.dropna(subset=["Latitude", "Longitude"]).copy()
    gdot = gdot.rename(
        columns={
            "Latitude": "Crash Latitude",
            "Longitude": "Crash Longitude",
        }
    )

    # Key Normalization
    tmas["TMAS Station ID"] = tmas["station_id"].astype(str).str.strip()
    tmas["join_id"] = normalize_station_id(tmas["TMAS Station ID"])

    tada["TADA Station ID"] = tada["Station ID"].astype(str).str.strip()
    tada["tada_join_id"] = normalize_station_id(tada["TADA Station ID"])

    # Coordinate Reprojection
    gdf_gdot = gpd.GeoDataFrame(
        gdot,
        geometry=gpd.points_from_xy(gdot["Crash Longitude"], gdot["Crash Latitude"]),
        crs="EPSG:4326",
    ).to_crs(UTM_ATL)

    gdf_tada = gpd.GeoDataFrame(
        tada.rename(
            columns={
                "Functional Class": "TADA Functional Class",
                "Latitude": "TADA Latitude",
                "Longitude": "TADA Longitude",
                "Station Type": "TADA Station Type",
                "Statistics type": "TADA Statistics Type",
            }
        ),
        geometry=gpd.points_from_xy(tada["Longitude"], tada["Latitude"]),
        crs="EPSG:4326",
    ).to_crs(UTM_ATL)

    gdf_tmas = gpd.GeoDataFrame(
        tmas.rename(
            columns={
                "latitude": "TMAS Latitude",
                "longitude": "TMAS Longitude",
                "functional_class": "TMAS Functional Class",
            }
        ),
        geometry=gpd.points_from_xy(tmas["longitude"], tmas["latitude"]),
        crs="EPSG:4326",
    ).to_crs(UTM_ATL)

    # Station link
    tmas_pts = gdf_tmas[
        [
            "TMAS Station ID",
            "join_id",
            "TMAS Latitude",
            "TMAS Longitude",
            "TMAS Functional Class",
            "geometry",
        ]
    ]

    exposure_by_station = link_tmas_to_tada(tmas_pts, gdf_tada)
    method_counts = exposure_by_station["TMAS_TADA_match_method"].value_counts().to_dict()

    tmas_for_crash = tmas_pts[["join_id", "TMAS Station ID", "geometry"]]
    crashes_with_station = gpd.sjoin_nearest(
        gdf_gdot,
        tmas_for_crash,
        how="inner",
        max_distance=BUFFER_M,
        distance_col="Crash_to_TMAS_m",
    ).drop(columns=["index_right"], errors="ignore")

    if crashes_with_station.index.duplicated().any():
        crashes_with_station = (
            crashes_with_station.sort_values("Crash_to_TMAS_m")
            .groupby(level=0, sort=False)
            .first()
        )

    crashes_df = pd.DataFrame(
        crashes_with_station.drop(columns=["geometry"], errors="ignore")
    )
    final_dashboard_df = crashes_df.merge(
        exposure_by_station.drop(columns=["TMAS Station ID"], errors="ignore"),
        on="join_id",
        how="inner",
        validate="many_to_one",
    )

    preferred = [
        "Date",
        "Time",
        "Hour",
        "Year",
        "Month",
        "Day",
        "DayOfWeek",
        "Crash Latitude",
        "Crash Longitude",
        "KABCO Severity",
        "# of Fatalities per Crash",
        "# Serious Injuries",
        "# Visible Injuries",
        "Geolocated City",
        "Geolocated County",
        "Roadway (From Crash Report)",
        "Intersection Name (from Crash Report)",
        "Manner of Collision (Crash Level)",
        "Location at Impact (Crash Level)",
        "Light Conditions (Crash Level)",
        "Weather Conditions (Crash Level)",
        "Surface Condition (Crash Level)",
        "Agency Name (Crash Level)",
        "Safety Equipment (Crash Level)",
        "First Harmful Event (Unit Order)",
        "Most Harmful Event (Crash Level)",
        "Operator/Pedestrian Contrib Factor (excl None, Other, No Contrib Factors)",
        "join_id",
        "TMAS Station ID",
        "TMAS Latitude",
        "TMAS Longitude",
        "TMAS Functional Class",
        "Crash_to_TMAS_m",
        "TMAS_TADA_match_method",
        "TADA Station ID",
        "tada_join_id",
        "TADA Latitude",
        "TADA Longitude",
        "TADA Functional Class",
        "TADA Year",
        "TADA Station Type",
        "TADA Statistics Type",
        "AADT",
        "Single-Unit Truck AADT",
        "Combo-Unit Truck AADT",
        "% Peak SU Trucks",
        "% Peak CU Trucks",
        "K-Factor",
        "D-Factor",
        "Future AADT",
        "TMAS_to_TADA_m",
    ]
    ordered = [c for c in preferred if c in final_dashboard_df.columns]
    ordered += [c for c in final_dashboard_df.columns if c not in ordered]
    final_dashboard_df = final_dashboard_df[ordered]

    final_dashboard_df.to_csv(OUT, index=False)
    print(
        f"Pipeline complete. {len(final_dashboard_df):,} incident rows linked via "
        f"join_id to {final_dashboard_df['join_id'].nunique()} TMAS stations."
        f"Output saved to {OUT}"
    )


if __name__ == "__main__":
    main()

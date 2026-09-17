"""Build a chart-ready station safety summary by hour, weekday, and month.

Each output row represents one station and one time bucket. Station attributes
are repeated on every row so the CSV can be used directly in a chart without
joining another table.
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "Atlanta_Commuter_Final_Dataset.csv"
VEHICLE_SRC = (
    ROOT
    / "raw_data"
    / "Travel_Monitoring_Analysis_System_(TMAS)_Traffic_Volume_2022_InsidePerimeter.csv"
)
OUT = ROOT / "condensed_data" / "station_safety_by_time.csv"
VEHICLE_CHUNK_SIZE = 200_000

STATION_COLS = [
    "join_id",
    "TMAS Station ID",
    "TMAS Latitude",
    "TMAS Longitude",
    "TMAS Functional Class",
]

TIME_GROUPS = [
    ("hour", "Hour", list(range(24))),
    (
        "day_of_week",
        "DayOfWeek",
        [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ],
    ),
    ("month", "Month", list(range(1, 13))),
]


def build_station_roster(df: pd.DataFrame) -> pd.DataFrame:
    """Return one row of static attributes for each matched station."""
    missing = [column for column in STATION_COLS if column not in df.columns]
    if missing:
        raise ValueError(f"Source data is missing station columns: {missing}")

    return (
        df[STATION_COLS]
        .drop_duplicates(subset=["join_id"])
        .sort_values("join_id")
        .reset_index(drop=True)
    )


def normalize_station_id(series: pd.Series) -> pd.Series:
    """Match the station-ID normalization used by the crash/station join."""
    normalized = series.astype("string").str.strip().str.replace('"', "", regex=False)
    normalized = normalized.str.replace("-", "", regex=False).str.lstrip("0")
    return normalized.replace("", "0")


def summarize_vehicle_counts(
    observations: pd.DataFrame, time_column: str
) -> pd.DataFrame:
    """Summarize observed vehicles and coverage for one time dimension."""
    summary = (
        observations.groupby(["join_id", time_column])
        .agg(
            vehicle_count=("vehicle_count", "sum"),
            traffic_observation_days=("date", "nunique"),
        )
        .reset_index()
        .rename(columns={time_column: "time_value"})
    )
    summary["vehicle_count"] = summary["vehicle_count"].astype("int64")
    summary["traffic_observation_days"] = summary[
        "traffic_observation_days"
    ].astype("int64")
    summary["average_vehicle_count"] = (
        summary["vehicle_count"] / summary["traffic_observation_days"]
    ).round(2)
    return summary


def build_vehicle_summaries() -> dict[str, pd.DataFrame]:
    """Aggregate lane/direction counts into chart-compatible time buckets."""
    use_columns = [
        "Station ID",
        "Year",
        "Month",
        "Day",
        "Hours",
        "Vehicle Count",
    ]
    station_hour_parts = []

    for chunk in pd.read_csv(
        VEHICLE_SRC,
        usecols=use_columns,
        dtype="string",
        chunksize=VEHICLE_CHUNK_SIZE,
    ):
        cleaned_counts = chunk["Vehicle Count"].str.replace(",", "", regex=False)
        chunk["vehicle_count"] = pd.to_numeric(cleaned_counts, errors="coerce")
        if chunk["vehicle_count"].isna().any():
            bad_values = chunk.loc[
                chunk["vehicle_count"].isna(), "Vehicle Count"
            ].unique()
            raise ValueError(f"Invalid TMAS vehicle counts: {bad_values.tolist()}")

        chunk["join_id"] = normalize_station_id(chunk["Station ID"])
        chunk["date"] = pd.to_datetime(
            chunk[["Year", "Month", "Day"]].rename(
                columns={"Year": "year", "Month": "month", "Day": "day"}
            ),
            errors="raise",
        )
        chunk["hour"] = pd.to_numeric(
            chunk["Hours"].str.extract(r"^(\d{1,2})", expand=False), errors="raise"
        ).astype("int64")
        if not chunk["hour"].between(0, 23).all():
            raise ValueError("TMAS Hours contains a value outside 00:00-23:00")

        # Sum lanes and directions to obtain the station-wide count for an hour.
        station_hour_parts.append(
            chunk.groupby(["join_id", "date", "hour"], as_index=False)[
                "vehicle_count"
            ].sum()
        )

    station_hour = (
        pd.concat(station_hour_parts, ignore_index=True)
        .groupby(["join_id", "date", "hour"], as_index=False)["vehicle_count"]
        .sum()
    )
    daily = (
        station_hour.groupby(["join_id", "date"], as_index=False)["vehicle_count"]
        .sum()
    )
    daily["day_of_week"] = daily["date"].dt.day_name()
    daily["month"] = daily["date"].dt.month

    return {
        "hour": summarize_vehicle_counts(station_hour, "hour"),
        "day_of_week": summarize_vehicle_counts(daily, "day_of_week"),
        "month": summarize_vehicle_counts(daily, "month"),
    }


def build_time_group(
    df: pd.DataFrame,
    stations: pd.DataFrame,
    vehicle_summary: pd.DataFrame,
    group_name: str,
    source_column: str,
    buckets: list,
) -> pd.DataFrame:
    """Aggregate safety measures and add explicit zero rows for empty buckets."""
    counts = (
        df.groupby(["join_id", source_column], dropna=False)
        .agg(
            total_crashes=("join_id", "size"),
            fatal_crashes=("_fatal_crash", "sum"),
            serious_injuries=("_serious_injuries", "sum"),
        )
        .reset_index()
        .rename(columns={source_column: "time_value"})
    )

    complete_index = pd.MultiIndex.from_product(
        [stations["join_id"], buckets], names=["join_id", "time_value"]
    ).to_frame(index=False)

    measures = complete_index.merge(
        counts, on=["join_id", "time_value"], how="left", validate="one_to_one"
    )
    measure_cols = ["total_crashes", "fatal_crashes", "serious_injuries"]
    measures[measure_cols] = measures[measure_cols].fillna(0).astype("int64")
    measures.insert(1, "time_period", group_name)

    result = stations.merge(
        measures, on="join_id", how="inner", validate="one_to_many"
    )
    result = result.merge(
        vehicle_summary,
        on=["join_id", "time_value"],
        how="left",
        validate="one_to_one",
    )
    result["traffic_observation_days"] = result[
        "traffic_observation_days"
    ].fillna(0).astype("int64")
    result["vehicle_count"] = result["vehicle_count"].astype("Int64")
    return result


def main() -> None:
    df = pd.read_csv(SRC)
    stations = build_station_roster(df)
    vehicle_summaries = build_vehicle_summaries()

    fatalities = pd.to_numeric(df["# of Fatalities per Crash"], errors="coerce")
    serious_injuries = pd.to_numeric(df["# Serious Injuries"], errors="coerce")
    df["_fatal_crash"] = fatalities.fillna(0).gt(0).astype("int64")
    df["_serious_injuries"] = serious_injuries.fillna(0).astype("int64")

    summaries = [
        build_time_group(
            df,
            stations,
            vehicle_summaries[group_name],
            group_name,
            source_column,
            buckets,
        )
        for group_name, source_column, buckets in TIME_GROUPS
    ]
    summary = pd.concat(summaries, ignore_index=True)

    final_columns = STATION_COLS + [
        "time_period",
        "time_value",
        "total_crashes",
        "fatal_crashes",
        "serious_injuries",
        "vehicle_count",
        "traffic_observation_days",
        "average_vehicle_count",
    ]
    summary = summary[final_columns]

    OUT.parent.mkdir(exist_ok=True)
    summary.to_csv(OUT, index=False)

    rows_by_period = summary.groupby("time_period", sort=False).size().to_dict()
    print(
        f"Created {OUT.name} with {len(summary):,} rows for "
        f"{len(stations)} stations: {rows_by_period}"
    )


if __name__ == "__main__":
    main()

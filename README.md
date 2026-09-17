# Traffic-Safety-Data

Crash-safety analysis for commuter routes inside Atlanta's I-285 perimeter.
Individual crash reports are joined to nearby traffic-count stations so each
crash can be analyzed alongside how much traffic (and truck traffic) normally
passes through that location.

## Pipeline

```
raw_data/
├── GDOT_Collisions_Dataset.csv            # raw crash reports (statewide GDOT data)
├── TADA_all_station_annualized_dataset.csv# annual traffic-volume stats per count station (statewide)
├── Travel_Monitoring_Analysis_System_(TMAS)_Traffic_Volume_2022_InsidePerimeter.csv
│                                             # observed hourly counts by station/lane/direction
└── tmas_stations_inside_perimeter.csv     # roster of the 30 count stations inside I-285

        │  scripts/join.py
        ▼

Atlanta_Commuter_Final_Dataset.csv          # one row per crash, enriched with its
                                             # nearest station's volume data

        │  scripts/condense_dataset.py
        ▼

condensed_data/
└── station_safety_by_time.csv  # chart-ready station safety totals by hour,
                                  # weekday, and month
```

`scripts/join.py` matches each crash to its nearest TMAS count station (within
250m) and that station's TADA annual-volume record (by ID, then by proximity).
`scripts/condense_dataset.py` creates one long-form, chart-ready table. Each
station has separate rows for all 24 hours, seven weekdays, and 12 months.
Every row includes the station attributes, total crashes, fatal crashes, and
the number of serious injuries. It replaces the general AADT field with the
2022 TMAS vehicle counts aggregated across lanes and directions. Empty crash
time buckets are retained with zeroes.
Filter `time_period` before summing the measures because every crash is counted
once in each of the hour, weekday, and month sections.

In `station_safety_by_time.csv`, `time_period` identifies whether `time_value`
is an hour (0-23), weekday name, or month number (1-12). `fatal_crashes` counts
crashes with at least one recorded fatality; `serious_injuries` sums injured
people rather than counting crashes.

`vehicle_count` is the exact sum of vehicles in the available TMAS observations
for that station and time bucket. `traffic_observation_days` records its
coverage. Because stations have between 7 and 365 observed days, use
`average_vehicle_count` to compare stations: it is the observed hourly average
for `hour` rows and the observed daily average for weekday and month rows.
Vehicle measures are blank when a station has no observation for a time bucket;
that represents missing coverage rather than zero traffic.

## Data dictionary: `Atlanta_Commuter_Final_Dataset.csv`

**Crash-level fields** (from GDOT, one value per crash):
| Column | Description |
|---|---|
| `Date` | Crash date |
| `Time` | Crash time (HH:MM or HH:MM:SS) |
| `Hour` | Hour of day the crash occurred (0-23), derived from `Time` |
| `Year`, `Month`, `Day`, `DayOfWeek` | Derived from `Date` |
| `Crash Latitude`, `Crash Longitude` | Crash location |
| `KABCO Severity` | Injury severity code: K=fatal, A=serious, B=minor/visible, C=possible/complaint, O=no injury |
| `# of Fatalities per Crash` | Fatality count |
| `# Serious Injuries` | Suspected-serious-injury count |
| `# Visible Injuries` | Visible-injury count |
| `Geolocated City`, `Geolocated County` | Geocoded location |
| `Roadway (From Crash Report)` | Road name from the report |
| `Intersection Name (from Crash Report)` | Nearest intersection, if any |
| `Manner of Collision (Crash Level)` | e.g. rear-end, angle, head-on |
| `Location at Impact (Crash Level)` | e.g. roadway, shoulder, median |
| `Light Conditions (Crash Level)` | Daylight, dark, dusk, etc. |
| `Weather Conditions (Crash Level)` | Clear, rain, fog, etc. |
| `Surface Condition (Crash Level)` | Dry, wet, ice, etc. |
| `Agency Name (Crash Level)` | Reporting law-enforcement agency |
| `Safety Equipment (Crash Level)` | Restraint/helmet use, if recorded |
| `First Harmful Event (Unit Order)` | First harmful event in the crash sequence |
| `Most Harmful Event (Crash Level)` | Most severe harmful event |
| `Operator/Pedestrian Contrib Factor (excl None, Other, No Contrib Factors)` | Recorded contributing factor, when present |

**Join keys:**
| Column | Description |
|---|---|
| `join_id` | Normalized TMAS station ID, used to link a crash to its matched station |
| `Crash_to_TMAS_m` | Distance in meters from the crash to its matched TMAS station (capped at 250m) |

**Station-level fields** (from TMAS/TADA, repeated for every crash matched to that station):
| Column | Description |
|---|---|
| `TMAS Station ID` | Count-station ID |
| `TMAS Latitude`, `TMAS Longitude` | Station location |
| `TMAS Functional Class` | Roadway functional classification at the station |
| `TMAS_TADA_match_method` | How this station's TADA volume record was found: `id_exact`, `id_suffix`, `spatial_same_fc`, or `spatial_nearest` |
| `TADA Station ID`, `tada_join_id` | Matched TADA station ID (raw and normalized) |
| `TADA Latitude`, `TADA Longitude` | TADA station location |
| `TADA Functional Class` | Roadway functional classification per TADA |
| `TADA Year` | Year of the TADA volume record |
| `TADA Station Type`, `TADA Statistics Type` | TADA station metadata |
| `AADT` | Annual Average Daily Traffic (average number of vehicles/day) |
| `Single-Unit Truck AADT`, `Combo-Unit Truck AADT` | Average daily truck volumes by truck type |
| `% Peak SU Trucks`, `% Peak CU Trucks` | Share of peak-hour traffic that is trucks |
| `K-Factor` | Ratio of peak-hour volume to AADT |
| `D-Factor` | Directional split of peak-hour traffic |
| `Future AADT` | Projected future AADT |
| `TMAS_to_TADA_m` | Distance in meters between the TMAS station and its matched TADA station |

## Notes / caveats

- The perimeter roster (`tmas_stations_inside_perimeter.csv`) has **30** count
  stations, but only **17** appear in `Atlanta_Commuter_Final_Dataset.csv`.
  All 30 stations do successfully match a TADA volume record — the other 13
  simply have no recorded crash within the 250m matching buffer, so they
  never end up as a crash's nearest station.
- `Single-Unit Truck AADT`, `Combo-Unit Truck AADT`, `% Peak SU Trucks`, and
  `% Peak CU Trucks` are missing for roughly half of crash rows (some TADA
  stations don't report truck-specific volumes).
- `Operator/Pedestrian Contrib Factor` is missing for about 15% of crashes
  (not every report captures a contributing factor).

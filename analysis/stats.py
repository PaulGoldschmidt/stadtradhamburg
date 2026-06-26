"""StadtRad Hamburg — extended descriptive statistics.

Companion to `analysis/trips.ipynb` (which produces the basic timeseries, top-
stations bar charts, duration histogram, and OD map). This script focuses on
the questions that notebook does NOT answer:

  - Individual outliers: longest, furthest, fastest single trips; bikes that
    moved the most (km, count, net displacement); bikes that never moved.
  - Aggregates: rentals per day, weekday × hour heatmap, top OD pairs, station
    net flow (sources & sinks).
  - Occupancy: stations that are chronically empty or chronically full.

Writes a Markdown report and a few PNGs to `analysis/out/`. Designed to be re-
run as the dataset grows.
"""

from __future__ import annotations

import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
# Auto-detect data layout — README mentions data/gbfs/, on-disk has data/.
_candidates = [REPO / "data" / "gbfs", REPO / "data"]
DATA = next(
    (c for c in _candidates if (c / "bikes").is_dir() and (c / "stations.parquet").is_file()),
    None,
)
if DATA is None:
    sys.exit(f"could not find bikes/ + stations.parquet under {_candidates}")

OUT = REPO / "analysis" / "out"
OUT.mkdir(parents=True, exist_ok=True)

LOCAL_TZ = "Europe/Berlin"
TRIP_CAP = "INTERVAL 6 HOUR"  # gaps longer than this are treated as offline, not a single ride

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------- setup ---------------------------------------------------------------

def build_views(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"CREATE VIEW bikes AS SELECT * FROM '{DATA / 'bikes' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(
        f"CREATE VIEW observations AS "
        f"SELECT * FROM '{DATA / 'observations' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(f"CREATE VIEW stations AS SELECT * FROM '{DATA / 'stations.parquet'}'")


def build_trips(con: duckdb.DuckDBPyConnection) -> None:
    """Reconstruct trips. A trip = bike's previous-seen station differs from its
    current-seen station, with a gap shorter than TRIP_CAP. The poll interval
    bounds the duration error at ±5 min on each endpoint."""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE trips AS
        WITH ordered AS (
          SELECT bike_number, station_id, observed_at,
                 LAG(station_id)  OVER w AS prev_station,
                 LAG(observed_at) OVER w AS prev_seen
          FROM bikes
          WHERE station_id IS NOT NULL
          WINDOW w AS (PARTITION BY bike_number ORDER BY observed_at)
        )
        SELECT bike_number,
               prev_station AS origin_id,
               station_id   AS destination_id,
               prev_seen    AS departed_after,
               observed_at  AS arrived_by,
               observed_at - prev_seen AS gap
        FROM ordered
        WHERE prev_station IS NOT NULL
          AND prev_station <> station_id
          AND observed_at - prev_seen < {TRIP_CAP}
        """
    )
    # Join in station metadata + compute haversine distance and speed in SQL.
    con.execute(
        """
        CREATE OR REPLACE TABLE trips_geo AS
        SELECT t.*,
               o.name AS origin_name, o.lat AS origin_lat, o.lon AS origin_lon,
               d.name AS dest_name,   d.lat AS dest_lat,   d.lon AS dest_lon,
               2 * 6371.0088 * asin(
                 sqrt(
                   pow(sin(radians(d.lat - o.lat) / 2), 2) +
                   cos(radians(o.lat)) * cos(radians(d.lat)) *
                   pow(sin(radians(d.lon - o.lon) / 2), 2)
                 )
               ) AS distance_km,
               epoch(arrived_by - departed_after) / 60.0 AS duration_min
        FROM trips t
        JOIN stations o ON o.station_id = t.origin_id
        JOIN stations d ON d.station_id = t.destination_id
        """
    )


# ---------- report helpers ------------------------------------------------------

def hr(title: str) -> None:
    print(f"\n## {title}\n")


def df_print(df: pd.DataFrame, floatfmt: str = "{:.2f}") -> None:
    if df.empty:
        print("_(empty)_")
        return
    # use pandas to_string for terminal-friendly output, with a numeric format.
    with pd.option_context(
        "display.max_columns", None,
        "display.max_rows", None,
        "display.width", 140,
        "display.float_format", floatfmt.format,
    ):
        print(df.to_string(index=False))


# ---------- sections ------------------------------------------------------------

def overview(con: duckdb.DuckDBPyConnection) -> dict:
    row = con.execute(
        """
        SELECT (SELECT COUNT(*) FROM bikes)                       AS bike_rows,
               (SELECT COUNT(*) FROM observations)                AS obs_rows,
               (SELECT COUNT(DISTINCT bike_number) FROM bikes)    AS unique_bikes,
               (SELECT COUNT(*) FROM stations)                    AS station_count,
               (SELECT COUNT(*) FROM trips_geo)                   AS trip_count,
               (SELECT MIN(observed_at) FROM bikes)               AS first_seen,
               (SELECT MAX(observed_at) FROM bikes)               AS last_seen
        """
    ).fetchone()
    bike_rows, obs_rows, bikes, stations_n, trips_n, first_seen, last_seen = row
    span_days = (last_seen - first_seen).total_seconds() / 86400.0

    hr("Dataset")
    print(f"- Window: **{first_seen}** → **{last_seen}** ({span_days:.1f} days, UTC)")
    print(f"- {bike_rows:,} bike snapshots, {obs_rows:,} station snapshots")
    print(f"- {bikes:,} unique bikes seen, {stations_n:,} stations")
    print(f"- {trips_n:,} trips reconstructed (gap < 6 h)")
    return {"trips": trips_n, "bikes": bikes, "stations": stations_n}


def rentals_per_day(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = con.execute(
        f"""
        SELECT CAST(arrived_by AT TIME ZONE '{LOCAL_TZ}' AS DATE) AS day,
               COUNT(*)                                            AS trips,
               COUNT(DISTINCT bike_number)                         AS unique_bikes
        FROM trips_geo
        GROUP BY 1 ORDER BY 1
        """
    ).df()

    hr("Rentals per day (Europe/Berlin)")
    print(f"Mean **{df['trips'].mean():,.0f}** trips/day  "
          f"(min {df['trips'].min():,}, max {df['trips'].max():,}, "
          f"median {df['trips'].median():,.0f}).")
    print()
    df_print(df.assign(weekday=pd.to_datetime(df["day"]).dt.day_name().str.slice(0, 3)))

    fig, ax = plt.subplots(figsize=(11, 4))
    ax.bar(df["day"], df["trips"], color="steelblue")
    ax.set_title("StadtRad Hamburg — trips per day (Europe/Berlin)")
    ax.set_xlabel("day")
    ax.set_ylabel("trips")
    ax.grid(axis="y", alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(OUT / "stats_daily.png", dpi=140)
    plt.close(fig)
    return df


def heatmap_hour_weekday(con: duckdb.DuckDBPyConnection) -> None:
    df = con.execute(
        f"""
        SELECT
          (EXTRACT(DOW FROM arrived_by AT TIME ZONE '{LOCAL_TZ}')::INT + 6) % 7 AS weekday,
          EXTRACT(HOUR FROM arrived_by AT TIME ZONE '{LOCAL_TZ}')::INT          AS hour,
          COUNT(*)                                                              AS trips
        FROM trips_geo
        GROUP BY 1, 2
        """
    ).df()
    # Pivot to 7 × 24 grid (Mon=0 … Sun=6). Empty cells → 0.
    grid = (
        df.pivot(index="weekday", columns="hour", values="trips")
        .reindex(index=range(7), columns=range(24))
        .fillna(0)
        .astype(int)
    )

    hr("Weekday × hour heatmap")
    print("Trip counts by weekday (rows) × hour-of-day (cols), local time.")
    print()
    df_print(
        grid.rename(index=dict(enumerate(WEEKDAY_NAMES))).reset_index().rename(
            columns={"weekday": "day"}
        ),
        floatfmt="{:.0f}",
    )

    fig, ax = plt.subplots(figsize=(12, 4))
    im = ax.imshow(grid.values, aspect="auto", cmap="viridis")
    ax.set_yticks(range(7), WEEKDAY_NAMES)
    ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
    ax.set_xlabel("hour (Europe/Berlin)")
    ax.set_title("StadtRad Hamburg — trips by weekday × hour")
    fig.colorbar(im, ax=ax, label="trips")
    fig.tight_layout()
    fig.savefig(OUT / "stats_heatmap.png", dpi=140)
    plt.close(fig)


def trip_distributions(con: duckdb.DuckDBPyConnection) -> None:
    stats = con.execute(
        """
        SELECT
          median(duration_min)                AS dur_p50,
          quantile_cont(duration_min, 0.9)    AS dur_p90,
          quantile_cont(duration_min, 0.99)   AS dur_p99,
          max(duration_min)                   AS dur_max,
          median(distance_km)                 AS dist_p50,
          quantile_cont(distance_km, 0.9)     AS dist_p90,
          quantile_cont(distance_km, 0.99)    AS dist_p99,
          max(distance_km)                    AS dist_max,
          median(distance_km / NULLIF(duration_min / 60.0, 0))                 AS speed_p50,
          quantile_cont(distance_km / NULLIF(duration_min / 60.0, 0), 0.9)     AS speed_p90
        FROM trips_geo
        WHERE distance_km > 0 AND duration_min > 0
        """
    ).fetchone()
    hr("Trip shape (duration, distance, average speed)")
    print(f"- Duration (min):   p50 **{stats[0]:.0f}**,  p90 {stats[1]:.0f},  p99 {stats[2]:.0f},  max {stats[3]:.0f}")
    print(f"- Distance (km):    p50 **{stats[4]:.2f}**,  p90 {stats[5]:.2f},  p99 {stats[6]:.2f},  max {stats[7]:.2f}")
    print(f"- Avg speed (km/h): p50 **{stats[8]:.2f}**,  p90 {stats[9]:.2f}")
    print()
    print(
        "Distance is straight-line origin→destination — actual ridden distance is "
        "longer. Speed is therefore a lower bound."
    )


def outlier_trips(con: duckdb.DuckDBPyConnection) -> None:
    hr("Outlier trips")

    def show(title: str, sql: str) -> None:
        print(f"\n### {title}\n")
        df = con.execute(sql).df()
        df_print(df)

    show(
        "Longest by duration (top 5)",
        """
        SELECT bike_number, origin_name, dest_name,
               duration_min, distance_km,
               departed_after AS departed_after_utc,
               arrived_by AS arrived_by_utc
        FROM trips_geo ORDER BY duration_min DESC LIMIT 5
        """,
    )
    show(
        "Furthest by distance (top 5)",
        """
        SELECT bike_number, origin_name, dest_name,
               distance_km, duration_min,
               distance_km / NULLIF(duration_min/60.0, 0) AS avg_kmh,
               arrived_by AS arrived_by_utc
        FROM trips_geo ORDER BY distance_km DESC LIMIT 5
        """,
    )
    print(
        "\n_Implausible speeds (>~30 km/h on a long leg) almost certainly mean the "
        "bike was moved by truck for rebalancing — not actually ridden. The "
        "next table filters those out._"
    )
    show(
        "Fastest sustained (≥2 km, ≥10 min — drops noisy poll-boundary trips)",
        """
        SELECT bike_number, origin_name, dest_name,
               distance_km, duration_min,
               distance_km / (duration_min/60.0) AS avg_kmh
        FROM trips_geo
        WHERE distance_km >= 2 AND duration_min >= 10
        ORDER BY avg_kmh DESC LIMIT 5
        """,
    )


def outlier_bikes(con: duckdb.DuckDBPyConnection) -> None:
    hr("Outlier bikes")

    def show(title: str, sql: str) -> None:
        print(f"\n### {title}\n")
        df = con.execute(sql).df()
        df_print(df)

    show(
        "Most rented (top 10)",
        """
        SELECT bike_number,
               COUNT(*) AS trips,
               SUM(distance_km) AS total_km,
               ROUND(AVG(distance_km), 2) AS avg_km,
               ROUND(AVG(duration_min), 1) AS avg_min
        FROM trips_geo GROUP BY 1 ORDER BY trips DESC LIMIT 10
        """,
    )
    show(
        "Most distance covered (top 10, sum of straight-line trip km)",
        """
        SELECT bike_number,
               ROUND(SUM(distance_km), 1) AS total_km,
               COUNT(*) AS trips,
               ROUND(AVG(distance_km), 2) AS avg_km
        FROM trips_geo GROUP BY 1 ORDER BY total_km DESC LIMIT 10
        """,
    )
    show(
        "Largest net displacement (start vs. end position, top 10)",
        """
        WITH pos AS (
          SELECT bike_number,
                 arg_min(lat, observed_at) AS first_lat,
                 arg_min(lon, observed_at) AS first_lon,
                 arg_max(lat, observed_at) AS last_lat,
                 arg_max(lon, observed_at) AS last_lon,
                 min(observed_at)          AS first_seen,
                 max(observed_at)          AS last_seen
          FROM bikes WHERE lat IS NOT NULL AND lon IS NOT NULL
          GROUP BY 1
        )
        SELECT bike_number,
               ROUND(2 * 6371.0088 * asin(
                 sqrt(
                   pow(sin(radians(last_lat - first_lat)/2), 2) +
                   cos(radians(first_lat)) * cos(radians(last_lat)) *
                   pow(sin(radians(last_lon - first_lon)/2), 2)
                 )
               ), 2) AS net_km,
               first_seen, last_seen
        FROM pos ORDER BY net_km DESC LIMIT 10
        """,
    )
    print(
        "\n_Three-digit `net_km` values are operator relocations (likely shipped in "
        "from / out to another city or a depot), not user trips._"
    )

    never_moved = con.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT bike_number
          FROM bikes WHERE station_id IS NOT NULL
          GROUP BY 1 HAVING COUNT(DISTINCT station_id) = 1
        )
        """
    ).fetchone()[0]
    total_bikes = con.execute(
        "SELECT COUNT(DISTINCT bike_number) FROM bikes WHERE station_id IS NOT NULL"
    ).fetchone()[0]
    print(
        f"\n**{never_moved:,}** of {total_bikes:,} ever-docked bikes "
        f"({never_moved / max(total_bikes, 1) * 100:.1f} %) "
        "were only seen at a single station for the whole window."
    )


def od_pairs(con: duckdb.DuckDBPyConnection) -> None:
    hr("Top origin → destination pairs")
    df = con.execute(
        """
        SELECT origin_name AS origin, dest_name AS destination,
               COUNT(*) AS trips,
               ROUND(AVG(distance_km), 2) AS avg_km,
               ROUND(AVG(duration_min), 1) AS avg_min
        FROM trips_geo
        GROUP BY 1, 2 ORDER BY trips DESC LIMIT 15
        """
    ).df()
    df_print(df)


def station_flow(con: duckdb.DuckDBPyConnection) -> None:
    hr("Station net flow (arrivals − departures)")
    print("Positive = bikes pile up here (sink); negative = bikes drain away (source).\n")
    df = con.execute(
        """
        WITH dep AS (SELECT origin_id AS sid, COUNT(*) AS departures FROM trips_geo GROUP BY 1),
             arr AS (SELECT destination_id AS sid, COUNT(*) AS arrivals FROM trips_geo GROUP BY 1)
        SELECT s.name AS station,
               COALESCE(dep.departures, 0) AS departures,
               COALESCE(arr.arrivals, 0)   AS arrivals,
               COALESCE(arr.arrivals, 0) - COALESCE(dep.departures, 0) AS net_flow
        FROM stations s
        LEFT JOIN dep ON dep.sid = s.station_id
        LEFT JOIN arr ON arr.sid = s.station_id
        WHERE COALESCE(dep.departures, 0) + COALESCE(arr.arrivals, 0) > 0
        ORDER BY net_flow DESC LIMIT 10
        """
    ).df()
    print("### Top sinks (net gainers)\n")
    df_print(df)
    df = con.execute(
        """
        WITH dep AS (SELECT origin_id AS sid, COUNT(*) AS departures FROM trips_geo GROUP BY 1),
             arr AS (SELECT destination_id AS sid, COUNT(*) AS arrivals FROM trips_geo GROUP BY 1)
        SELECT s.name AS station,
               COALESCE(dep.departures, 0) AS departures,
               COALESCE(arr.arrivals, 0)   AS arrivals,
               COALESCE(arr.arrivals, 0) - COALESCE(dep.departures, 0) AS net_flow
        FROM stations s
        LEFT JOIN dep ON dep.sid = s.station_id
        LEFT JOIN arr ON arr.sid = s.station_id
        WHERE COALESCE(dep.departures, 0) + COALESCE(arr.arrivals, 0) > 0
        ORDER BY net_flow ASC LIMIT 10
        """
    ).df()
    print("\n### Top sources (net losers)\n")
    df_print(df)


def chronic_occupancy(con: duckdb.DuckDBPyConnection) -> None:
    hr("Chronic occupancy — frustrated users vs. wasted docks")
    df = con.execute(
        """
        SELECT s.name AS station,
               COUNT(*) AS samples,
               ROUND(AVG(CASE WHEN o.num_bikes_available = 0 THEN 1.0 ELSE 0 END) * 100, 1) AS pct_empty,
               ROUND(AVG(CASE WHEN o.num_docks_available = 0 THEN 1.0 ELSE 0 END) * 100, 1) AS pct_full,
               s.capacity
        FROM observations o JOIN stations s ON s.station_id = o.station_id
        WHERE o.is_installed
        GROUP BY 1, 5
        HAVING samples > 100
        """
    ).df()
    print("### Most often empty (no bikes — would-be riders turned away)\n")
    df_print(df.sort_values("pct_empty", ascending=False).head(10))
    print("\n### Most often full (no docks — would-be returners turned away)\n")
    df_print(df.sort_values("pct_full", ascending=False).head(10))


# ---------- main ----------------------------------------------------------------

def main() -> int:
    con = duckdb.connect()
    build_views(con)
    print("# StadtRad Hamburg — extended descriptive statistics\n")
    print("_Generated by `analysis/stats.py`. Companion to `analysis/trips.ipynb`._\n")
    print(f"Data root: `{DATA.relative_to(REPO)}/`")

    print("\nBuilding trips table…", flush=True, file=sys.stderr)
    build_trips(con)

    overview(con)
    rentals_per_day(con)
    heatmap_hour_weekday(con)
    trip_distributions(con)
    od_pairs(con)
    station_flow(con)
    outlier_trips(con)
    outlier_bikes(con)
    chronic_occupancy(con)

    print("\n---\n")
    print(f"Figures written to `{OUT.relative_to(REPO)}/`:")
    for p in sorted(OUT.glob("stats_*.png")):
        print(f"- `{p.name}`")
    return 0


if __name__ == "__main__":
    # Tee stdout to a markdown file for easy sharing.
    report_path = OUT / "stats_report.md"
    buf = StringIO()

    class Tee:
        def __init__(self, *streams):
            self.streams = streams
        def write(self, s):
            for st in self.streams:
                st.write(s)
        def flush(self):
            for st in self.streams:
                st.flush()

    with redirect_stdout(Tee(sys.stdout, buf)):
        rc = main()
    report_path.write_text(buf.getvalue())
    print(f"\n(report also saved to {report_path.relative_to(REPO)})", file=sys.stderr)
    sys.exit(rc)

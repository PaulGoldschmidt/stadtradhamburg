"""Shared helpers: data path autodetect, DuckDB view setup, trip reconstruction.

Imported by `analysis/stats.py` and the `analysis/clip/*` pipeline so both share
a single source of truth for what a trip *is*.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent

LOCAL_TZ = "Europe/Berlin"
TRIP_CAP = "INTERVAL 6 HOUR"  # gaps longer than this are treated as offline, not one ride


def data_root() -> Path:
    """README mentions data/gbfs/; the on-disk layout is data/. Try both."""
    for candidate in (REPO / "data" / "gbfs", REPO / "data"):
        if (candidate / "bikes").is_dir() and (candidate / "stations.parquet").is_file():
            return candidate
    sys.exit(
        "could not find bikes/ + stations.parquet under data/ or data/gbfs/"
    )


def build_views(con: duckdb.DuckDBPyConnection, data: Path | None = None) -> Path:
    data = data or data_root()
    con.execute(
        f"CREATE VIEW bikes AS SELECT * FROM '{data / 'bikes' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(
        f"CREATE VIEW observations AS "
        f"SELECT * FROM '{data / 'observations' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(
        f"CREATE VIEW stations AS SELECT * FROM '{data / 'stations.parquet'}'"
    )
    return data


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

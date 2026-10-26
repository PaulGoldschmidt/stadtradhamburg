"""Shared helpers: data path autodetect, DuckDB setup, partition listing, and
the single definition of what a trip *is*.

Imported by `analysis/stats.py`, the `analysis/clip/*` pipeline, the forecast,
the incremental deriver (`scripts/derive.py`) and the web layer, so all of them
share one source of truth for trips and for where the data lives.
"""

from __future__ import annotations

import os
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


def partition_dates(data_dir: Path, stream: str) -> list[str]:
    """Sorted `date=` partition values (ISO dates) of a stream (`observations`
    or `bikes`) from a plain directory listing — far cheaper than having DuckDB
    glob tens of thousands of files. Empty when the stream has no partition yet."""
    base = data_dir / stream
    if not base.is_dir():
        return []
    return sorted(
        p.name.split("=", 1)[1]
        for p in base.iterdir()
        if p.is_dir() and p.name.startswith("date=")
    )


def configure_duckdb(
    con: duckdb.DuckDBPyConnection,
    *,
    memory_limit: str | None = None,
    threads: int | None = None,
    temp_dir: Path | str | None = None,
) -> None:
    """Bound a connection's memory and CPU. The environment variables
    DUCKDB_MEMORY_LIMIT, DUCKDB_THREADS and DUCKDB_TEMP_DIR override the
    arguments, so containers can be tuned without code changes. Anything beyond
    `memory_limit` spills to `temp_dir` (disk is cheap here, RAM is not)."""
    limit = os.environ.get("DUCKDB_MEMORY_LIMIT") or memory_limit
    nthreads = os.environ.get("DUCKDB_THREADS") or threads
    tmp = os.environ.get("DUCKDB_TEMP_DIR") or temp_dir
    if limit:
        con.execute(f"SET memory_limit = '{limit}'")
    if nthreads:
        con.execute(f"SET threads = {int(nthreads)}")
    if tmp:
        Path(tmp).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory = '{tmp}'")


def build_views(con: duckdb.DuckDBPyConnection, data: Path | None = None) -> Path:
    data = data or data_root()
    con.execute(
        f"CREATE OR REPLACE VIEW bikes AS "
        f"SELECT * FROM '{data / 'bikes' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW observations AS "
        f"SELECT * FROM '{data / 'observations' / 'date=*' / 'part-*.parquet'}'"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW stations AS SELECT * FROM '{data / 'stations.parquet'}'"
    )
    return data


# ---------- trip definition -----------------------------------------------------
#
# A trip = a bike whose previously-seen station differs from its currently-seen
# station, with a gap shorter than TRIP_CAP. Sightings without a station (bike
# in use or free-floating) are ignored entirely, so "previous" always means the
# previous *docked* sighting. The poll interval bounds the duration error at
# ±5 min on each endpoint.
#
# Both the full rebuild (`build_trips`, used offline) and the incremental
# deriver (`scripts/derive.py`, used in production) produce trips from an
# `ordered` relation with the columns bike_number, station_id, observed_at,
# prev_station, prev_seen — only how `prev_*` is obtained differs.

TRIP_WHERE = f"""prev_station IS NOT NULL
          AND prev_station <> station_id
          AND prev_seen < observed_at
          AND observed_at - prev_seen < {TRIP_CAP}"""

TRIP_COLUMNS = ("bike_number", "origin_id", "destination_id", "departed_after", "arrived_by")


def trips_select(ordered: str) -> str:
    """SELECT turning an `ordered` relation into trip rows (plus `gap`)."""
    return f"""
        SELECT bike_number,
               prev_station AS origin_id,
               station_id   AS destination_id,
               prev_seen    AS departed_after,
               observed_at  AS arrived_by,
               observed_at - prev_seen AS gap
        FROM {ordered}
        WHERE {TRIP_WHERE}
    """


def trips_geo_select(trips: str = "trips") -> str:
    """SELECT joining trips to station coordinates, with haversine distance and
    duration in minutes."""
    return f"""
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
        FROM {trips} t
        JOIN stations o ON o.station_id = t.origin_id
        JOIN stations d ON d.station_id = t.destination_id
    """


def build_trips(con: duckdb.DuckDBPyConnection) -> None:
    """Full rebuild of `trips` and `trips_geo` from the whole `bikes` history.
    Sorts every sighting ever recorded, so it needs memory proportional to the
    history: fine offline (stats, clip, the equivalence check), not for the
    production web — that reads the incrementally derived files instead."""
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
        {trips_select("ordered")}
        """
    )
    con.execute("CREATE OR REPLACE TABLE trips_geo AS " + trips_geo_select("trips"))

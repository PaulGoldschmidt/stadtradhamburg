"""Single lock-guarded, read-only DuckDB connection over the Parquet store.

Views (`bikes`, `observations`, `stations`, optional `weather`/`forecast_live`)
are globs, so they pick up new poller output at query time. But the store is
~20k small Parquet files, so any query that scans a view whole costs 0.5–3 s —
far too slow for a request. Every hot query therefore reads one of the small
tables materialized here instead:

- `trips_geo`      — trip reconstruction (~8 s), rebuilt at startup and every
                     TRIP_REFRESH_SEC by the app's background task.
- `obs_latest`     — current status per station, derived from the newest two
                     date partitions only (~0.1 s), rebuilt every LIVE_REFRESH_SEC.
- `bike_last`      — per-bike first/last sighting + last docked station.
- `obs_meta`       — dataset-wide near-constants (first observation, station count).
- `station_hourly` — avg bikes by (station, hour), feeds the station panel.

All access is serialized through one lock: DuckDB connections are not safe for
concurrent use, and `PRAGMA threads` bounds CPU per query. Because nearly every
read is TTL-cached upstream and the app pre-warms those caches after each
refresh, lock contention is negligible in practice.
"""

from __future__ import annotations

import logging
import threading

import duckdb
import pandas as pd

from . import settings

log = logging.getLogger("web.db")

_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()
_max_obs_date: str | None = None

HAS_WEATHER = False
HAS_FORECAST = False


def _ensure_optional_views(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the weather / forecast views if their files exist now. Called at
    startup and on every refresh so the site lights up once those artifacts land."""
    global HAS_WEATHER, HAS_FORECAST
    from weather import build_weather_view, weather_path

    wp = weather_path(settings.data_dir())
    if wp.exists():
        build_weather_view(con, wp)
        HAS_WEATHER = True
    fl = settings.FORECAST_LIVE
    if fl.exists():
        con.execute(f"CREATE OR REPLACE VIEW forecast_live AS SELECT * FROM '{fl}'")
        HAS_FORECAST = True


def _obs_partition_dates() -> list[str]:
    """Sorted `date=` partition values of the observations stream, from a plain
    directory listing — far cheaper than having DuckDB glob ~10k files."""
    base = settings.data_dir() / "observations"
    return sorted(p.name.split("=", 1)[1] for p in base.iterdir() if p.name.startswith("date="))


def _refresh_latest(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)materialize `obs_latest`. The poller snapshots every station each
    cycle, so the newest two partitions always contain each station's latest
    report; the literal date filter lets DuckDB skip the other ~10k files."""
    global _max_obs_date
    dates = _obs_partition_dates()
    if not dates:
        return
    _max_obs_date = dates[-1]
    con.execute(
        f"""
        CREATE OR REPLACE TABLE obs_latest AS
        SELECT station_id,
               arg_max(num_bikes_available, last_reported) AS bikes,
               arg_max(num_docks_available, last_reported) AS docks,
               arg_max(is_installed, last_reported)        AS installed,
               max(last_reported)                          AS as_of
        FROM observations
        WHERE date >= DATE '{dates[-2] if len(dates) > 1 else dates[-1]}'
        GROUP BY 1
        """
    )


def _refresh_history_tables(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)materialize the full-scan aggregates. Runs with the heavy trip
    refresh, so the 1 GB+ `bikes`/`observations` globs are scanned once per
    TRIP_REFRESH_SEC instead of once per request."""
    con.execute(
        """
        CREATE OR REPLACE TABLE bike_last AS
        SELECT bike_number,
               min(observed_at) AS first_seen,
               max(observed_at) AS last_seen,
               max(observed_at) FILTER (WHERE station_id IS NOT NULL) AS last_docked,
               arg_max(station_id, observed_at) FILTER (WHERE station_id IS NOT NULL) AS station_id
        FROM bikes
        GROUP BY 1
        """
    )
    dates = _obs_partition_dates()
    first = f"WHERE date = DATE '{dates[0]}'" if dates else ""
    con.execute(
        f"""
        CREATE OR REPLACE TABLE obs_meta AS
        SELECT (SELECT min(last_reported) FROM observations {first}) AS first_observation,
               (SELECT count(DISTINCT station_id) FROM observations) AS stations
        """
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE station_hourly AS
        SELECT station_id,
               EXTRACT(hour FROM last_reported AT TIME ZONE '{settings.LOCAL_TZ}')::INT AS hour,
               round(avg(num_bikes_available), 2) AS avg_bikes
        FROM observations
        WHERE is_installed
        GROUP BY 1, 2
        """
    )


def init() -> None:
    """Connect, build views, and warm the trip cache. Call once at app startup."""
    global _conn
    from _trips import build_views

    con = duckdb.connect(database=":memory:")
    con.execute(f"PRAGMA threads={settings.DUCKDB_THREADS}")
    build_views(con, settings.data_dir())
    _ensure_optional_views(con)
    _conn = con
    log.info("duckdb connected (threads=%d, weather=%s, forecast=%s)",
             settings.DUCKDB_THREADS, HAS_WEATHER, HAS_FORECAST)
    refresh_trips()


def refresh_trips() -> None:
    """Rebuild `trips`/`trips_geo` and all materialized aggregates (the expensive
    op, ~10 s) under the lock."""
    from _trips import build_trips

    assert _conn is not None, "db.init() must run first"
    with _lock:
        _ensure_optional_views(_conn)
        build_trips(_conn)
        _refresh_latest(_conn)
        _refresh_history_tables(_conn)
    log.info("trip cache refreshed")


def refresh_live() -> None:
    """Cheap between-trips refresh: only re-derive the current station status."""
    assert _conn is not None, "db.init() must run first"
    with _lock:
        _refresh_latest(_conn)


def max_obs_date() -> str | None:
    """Newest observations partition date (as of the last refresh) — for pruning
    partition scans with a literal predicate."""
    return _max_obs_date


def query_df(sql: str, params: list | None = None) -> pd.DataFrame:
    assert _conn is not None, "db.init() must run first"
    with _lock:
        return _conn.execute(sql, params or []).df()

"""Single lock-guarded, read-only DuckDB connection over the Parquet store.

Views (`bikes`, `observations`, `stations`, optional `weather`/`forecast_live`)
are globs, so they pick up new poller output at query time. But the store is
~20k small Parquet files, so any query that scans a view whole costs 0.5–3 s —
far too slow for a request. Every hot query therefore reads one of the small
tables materialized here instead:

- `trips_geo`      — trip reconstruction (~8 s), rebuilt at startup and every
                     TRIP_REFRESH_SEC by the app's background task.
- `obs_latest`     — latest report per station, for every station ever
                     observed. Seeded from full history once, then kept current
                     by merging in the newest date partitions (~0.1 s) every
                     LIVE_REFRESH_SEC, so stations keep their last-known status
                     even after dropping out of the feed.
- `bike_last`      — per-bike first/last sighting + last docked station,
                     rebuilt each heavy cycle (feeds the live fleet counts).
- `obs_meta`       — dataset-wide near-constants (first observation, station
                     count), rebuilt only when a new date partition appears.
- `station_hourly` — avg bikes by (station, hour) for the station panel,
                     same daily cadence as obs_meta.

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
_slow_tables_for: str | None = None  # newest partition the daily tables were built for

HAS_WEATHER = False
HAS_FORECAST = False

# Latest report per station; `{where}` prunes the partition glob.
_OBS_LATEST_SELECT = """
    SELECT station_id,
           arg_max(num_bikes_available, last_reported) AS bikes,
           arg_max(num_docks_available, last_reported) AS docks,
           arg_max(is_installed, last_reported)        AS installed,
           max(last_reported)                          AS as_of
    FROM observations{where}
    GROUP BY 1
"""


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
    directory listing — far cheaper than having DuckDB glob ~10k files. Empty
    when the poller hasn't produced a cycle yet."""
    base = settings.data_dir() / "observations"
    if not base.is_dir():
        return []
    return sorted(p.name.split("=", 1)[1] for p in base.iterdir() if p.name.startswith("date="))


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0])


def _refresh_latest(con: duckdb.DuckDBPyConnection, dates: list[str]) -> None:
    """Materialize/update `obs_latest`. The first build scans the full history;
    after that only the newest two partitions are re-scanned and merged in (the
    poller snapshots every station each cycle, so they contain every current
    station's latest report). Stations absent from the feed keep their
    last-known row instead of vanishing."""
    global _max_obs_date
    if not dates:
        # Empty store: serve empty results rather than catalog errors.
        con.execute(
            """CREATE TABLE IF NOT EXISTS obs_latest (
                 station_id VARCHAR, bikes BIGINT, docks BIGINT,
                 installed BOOLEAN, as_of TIMESTAMPTZ)"""
        )
        return
    _max_obs_date = dates[-1]
    if not _table_exists(con, "obs_latest"):
        con.execute("CREATE TABLE obs_latest AS " + _OBS_LATEST_SELECT.format(where=""))
        return
    lo = dates[-2] if len(dates) > 1 else dates[-1]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE obs_fresh AS "
        + _OBS_LATEST_SELECT.format(where=f" WHERE date >= DATE '{lo}'")
    )
    con.execute("DELETE FROM obs_latest WHERE station_id IN (SELECT station_id FROM obs_fresh)")
    con.execute("INSERT INTO obs_latest SELECT * FROM obs_fresh")
    con.execute("DROP TABLE obs_fresh")


def _refresh_history_tables(con: duckdb.DuckDBPyConnection, dates: list[str]) -> None:
    """(Re)materialize the full-scan aggregates. `bike_last` rebuilds every heavy
    cycle (it feeds the live fleet counts); the slow-moving `obs_meta` and
    `station_hourly` rebuild only when a new date partition appears, so the
    5-minute refresh doesn't re-scan the whole store for near-constant results."""
    global _slow_tables_for
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
    if not dates:
        con.execute(
            "CREATE TABLE IF NOT EXISTS obs_meta (first_observation TIMESTAMPTZ, stations BIGINT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS station_hourly (station_id VARCHAR, hour INT, avg_bikes DOUBLE)"
        )
        return
    if _slow_tables_for == dates[-1]:
        return
    # The poller partitions rows by each observation's own UTC date, so the
    # dataset's first observation always lives in the first partition.
    con.execute(
        f"""
        CREATE OR REPLACE TABLE obs_meta AS
        SELECT (SELECT min(last_reported) FROM observations WHERE date = DATE '{dates[0]}') AS first_observation,
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
    _slow_tables_for = dates[-1]


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
    """Rebuild `trips`/`trips_geo` and the materialized aggregates (the expensive
    op, ~10 s) under the lock."""
    from _trips import build_trips

    assert _conn is not None, "db.init() must run first"
    with _lock:
        _ensure_optional_views(_conn)
        build_trips(_conn)
        dates = _obs_partition_dates()
        _refresh_latest(_conn, dates)
        _refresh_history_tables(_conn, dates)
    log.info("trip cache refreshed")


def refresh_live() -> None:
    """Cheap between-trips refresh: merge the newest partitions into obs_latest."""
    assert _conn is not None, "db.init() must run first"
    with _lock:
        _refresh_latest(_conn, _obs_partition_dates())


def max_obs_date() -> str | None:
    """Newest observations partition date (as of the last refresh) — for pruning
    partition scans with a literal predicate."""
    return _max_obs_date


def query_df(sql: str, params: list | None = None) -> pd.DataFrame:
    assert _conn is not None, "db.init() must run first"
    with _lock:
        return _conn.execute(sql, params or []).df()

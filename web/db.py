"""Single lock-guarded, read-only DuckDB connection over the Parquet store and
the derived store.

The web process holds no derived state and rebuilds nothing. Everything the
site needs beyond the raw streams is produced incrementally by
`scripts/derive.py` (the `deriver` container) as small Parquet files under
`derived/` — see `analysis/derived.py` for the layout — and exposed here as
views:

- `trips` / `trips_geo`  — reconstructed trips (by arrival day) joined to stations
- `obs_latest`           — latest report per station ever observed
- `bike_last`            — per-bike first/last sighting + last docked station
- `obs_meta`             — first observation, distinct station count
- `station_hourly`       — avg bikes by (station, local hour)

plus the raw views `bikes`, `observations`, `stations` and the optional
`weather` / `forecast_live`. Views over globs resolve at query time, so new
files written by the deriver, the poller or the forecast are picked up on the
next query; the only periodic work here is re-creating the views every
LIVE_REFRESH_SEC so files that appear for the first time (first start before
the deriver's bootstrap) get a real view instead of the empty placeholder.

Memory stays small by design: one thread, a bounded buffer pool
(DUCKDB_MEMORY_LIMIT) that spills to DUCKDB_TEMP_DIR, and queries that only
touch the derived files — the raw store is scanned solely by
`station_detail`'s partition-pruned timeline and the occupancy download. All
access is serialized through one lock (DuckDB connections are not safe for
concurrent use); the TTL caches in `queries.py` keep lock contention
negligible.
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
DERIVED: dict[str, bool] = {}


def _ensure_optional_views(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the weather / forecast views if their files exist now, so the
    site lights up once those artifacts land."""
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


def _refresh(con: duckdb.DuckDBPyConnection) -> None:
    """Re-create every file-dependent view and note the newest partition."""
    global _max_obs_date, DERIVED
    from _trips import partition_dates
    from derived import build_derived_views

    _ensure_optional_views(con)
    present = build_derived_views(con, settings.derived_dir(), settings.LOCAL_TZ)
    if present != DERIVED:
        missing = [k for k, v in present.items() if not v]
        log.info("derived store %s: %s", settings.derived_dir(),
                 "complete" if not missing else f"missing {missing} (deriver not run yet?)")
        DERIVED = present
    dates = partition_dates(settings.data_dir(), "observations")
    _max_obs_date = dates[-1] if dates else None


def init() -> None:
    """Connect and build the views. Call once at app startup. Cheap: no scan."""
    global _conn
    from _trips import build_views, configure_duckdb

    con = duckdb.connect(database=":memory:")
    configure_duckdb(
        con,
        memory_limit=settings.DUCKDB_MEMORY_LIMIT,
        threads=settings.DUCKDB_THREADS,
        temp_dir=settings.DUCKDB_TEMP_DIR,
    )
    build_views(con, settings.data_dir())
    _refresh(con)
    _conn = con
    log.info("duckdb connected (threads=%d, memory_limit=%s, weather=%s, forecast=%s)",
             settings.DUCKDB_THREADS, settings.DUCKDB_MEMORY_LIMIT, HAS_WEATHER, HAS_FORECAST)


def refresh_views() -> None:
    """Periodic: pick up derived/forecast/weather files that appeared since."""
    assert _conn is not None, "db.init() must run first"
    with _lock:
        _refresh(_conn)


def max_obs_date() -> str | None:
    """Newest observations partition date (as of the last refresh) — for pruning
    partition scans with a literal predicate."""
    return _max_obs_date


def query_df(sql: str, params: list | None = None) -> pd.DataFrame:
    assert _conn is not None, "db.init() must run first"
    with _lock:
        return _conn.execute(sql, params or []).df()

"""Single lock-guarded, read-only DuckDB connection over the Parquet store.

Views (`bikes`, `observations`, `stations`, optional `weather`/`forecast_live`)
are globs, so they pick up new poller output at query time. The one materialized
table is `trips_geo` (trip reconstruction, ~15 s) — rebuilt once at startup and
periodically by the app's background task, so requests never pay that cost.

All access is serialized through one lock: DuckDB connections are not safe for
concurrent use, and `PRAGMA threads` bounds CPU per query. Because nearly every
read is TTL-cached upstream, lock contention is negligible in practice.
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
    """Rebuild the `trips`/`trips_geo` tables (the expensive op) under the lock."""
    from _trips import build_trips

    assert _conn is not None, "db.init() must run first"
    with _lock:
        _ensure_optional_views(_conn)
        build_trips(_conn)
    log.info("trip cache refreshed")


def query_df(sql: str, params: list | None = None) -> pd.DataFrame:
    assert _conn is not None, "db.init() must run first"
    with _lock:
        return _conn.execute(sql, params or []).df()

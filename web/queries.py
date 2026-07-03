"""All SQL lives here. Each public function is TTL-cached and returns
JSON-native structures (via `records()`), so route handlers stay trivial.

Trip-derived queries read the cached `trips_geo` table (see db.py); station/live
queries read the Parquet-backed views. SQL mirrors analysis/stats.py so the site
and the offline reports agree on definitions.
"""

from __future__ import annotations

import json

import pandas as pd

from . import db, settings
from .cache import ttl_cache

TZ = settings.LOCAL_TZ


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → list of JSON-native dicts (Timestamps→ISO, NaN→null, numpy→native)."""
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _one(df: pd.DataFrame) -> dict:
    rows = records(df)
    return rows[0] if rows else {}


@ttl_cache(settings.TTL_LIVE)
def latest_day() -> str:
    """Latest local date present in the data — used as 'today' so the site works
    on both the frozen dataset and a live poller-fed one."""
    df = db.query_df(
        f"SELECT CAST(max(last_reported) AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS d FROM observations"
    )
    return str(df["d"].iloc[0])


# ---------- live ----------------------------------------------------------------

@ttl_cache(settings.TTL_LIVE)
def health() -> dict:
    df = db.query_df(
        f"""
        SELECT max(last_reported) AS last_observation,
               min(last_reported) AS first_observation,
               count(DISTINCT station_id) AS stations
        FROM observations
        """
    )
    out = _one(df)
    out["latest_day"] = latest_day()
    out["has_weather"] = db.HAS_WEATHER
    out["has_forecast"] = db.HAS_FORECAST
    return out


@ttl_cache(settings.TTL_LIVE)
def stats_today() -> dict:
    day = latest_day()
    trips = _one(db.query_df(
        f"""
        WITH t AS (
          SELECT * FROM trips_geo
          WHERE CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) = DATE '{day}'
        )
        SELECT (SELECT count(*) FROM t)                              AS trips,
               (SELECT count(DISTINCT bike_number) FROM t)          AS bikes_moved,
               (SELECT COALESCE(round(sum(distance_km), 0), 0) FROM t) AS km,
               (SELECT COALESCE(round(avg(duration_min), 1), 0) FROM t) AS avg_min
        """
    ))
    fleet = _one(db.query_df(
        """
        WITH last AS (SELECT max(observed_at) AS t FROM bikes),
             recent AS (
               SELECT DISTINCT bike_number FROM bikes, last
               WHERE observed_at >= last.t - INTERVAL 24 HOUR
             )
        SELECT (SELECT count(*) FROM recent) AS fleet_recent,
               (SELECT count(*) FROM bikes b, last WHERE b.observed_at = last.t) AS visible_now
        """
    ))
    stations = _one(db.query_df(
        """
        WITH latest AS (
          SELECT station_id,
                 arg_max(num_bikes_available, last_reported) AS bikes,
                 arg_max(num_docks_available, last_reported) AS docks,
                 arg_max(is_installed, last_reported)        AS installed
          FROM observations GROUP BY 1
        )
        SELECT sum((bikes = 0 AND installed)::INT) AS stations_empty_now,
               sum((docks = 0 AND installed)::INT) AS stations_full_now,
               sum(installed::INT)                 AS stations_online
        FROM latest
        """
    ))
    busiest = _one(db.query_df(
        f"""
        WITH t AS (
          SELECT * FROM trips_geo
          WHERE CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) = DATE '{day}'
        ),
        flow AS (SELECT origin_id AS sid FROM t UNION ALL SELECT destination_id FROM t)
        SELECT s.name AS busiest_station, count(*) AS moves
        FROM flow JOIN stations s ON s.station_id = flow.sid
        GROUP BY 1 ORDER BY moves DESC LIMIT 1
        """
    ))
    bikes_in_use = max(int(fleet.get("fleet_recent") or 0) - int(fleet.get("visible_now") or 0), 0)
    return {
        "day": day,
        **trips,
        "bikes_in_use": bikes_in_use,
        **stations,
        "busiest_station": busiest.get("busiest_station"),
    }


@ttl_cache(settings.TTL_LIVE)
def stations_live() -> list[dict]:
    forecast_sel = "f.p_empty" if db.HAS_FORECAST else "CAST(NULL AS DOUBLE) AS p_empty"
    forecast_join = "LEFT JOIN forecast_live f ON f.station_id = s.station_id" if db.HAS_FORECAST else ""
    df = db.query_df(
        f"""
        WITH latest AS (
          SELECT station_id,
                 arg_max(num_bikes_available, last_reported) AS bikes,
                 arg_max(num_docks_available, last_reported) AS docks,
                 arg_max(is_installed, last_reported)        AS installed
          FROM observations GROUP BY 1
        )
        SELECT s.station_id, s.name, s.lat, s.lon,
               l.bikes, l.docks, l.installed,
               (l.bikes + l.docks) AS capacity_now,
               CASE WHEN (l.bikes + l.docks) > 0
                    THEN round(l.bikes::DOUBLE / (l.bikes + l.docks), 3) END AS occ,
               {forecast_sel}
        FROM stations s
        JOIN latest l ON l.station_id = s.station_id
        {forecast_join}
        WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL
        """
    )
    return records(df)


# ---------- history -------------------------------------------------------------

@ttl_cache(settings.TTL_HISTORY)
def history_summary() -> dict:
    return _one(db.query_df(
        """
        SELECT (SELECT count(*) FROM trips_geo)                    AS trips,
               (SELECT count(DISTINCT bike_number) FROM bikes)     AS unique_bikes,
               (SELECT count(*) FROM stations)                     AS stations,
               (SELECT min(observed_at) FROM bikes)                AS first_seen,
               (SELECT max(observed_at) FROM bikes)                AS last_seen
        """
    ))


@ttl_cache(settings.TTL_HISTORY)
def trips_daily() -> list[dict]:
    return records(db.query_df(
        f"""
        SELECT CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS day,
               count(*)                                              AS trips,
               count(DISTINCT bike_number)                           AS unique_bikes
        FROM trips_geo GROUP BY 1 ORDER BY 1
        """
    ))


@ttl_cache(settings.TTL_HISTORY)
def heatmap() -> dict:
    df = db.query_df(
        f"""
        SELECT (EXTRACT(dow FROM arrived_by AT TIME ZONE '{TZ}')::INT + 6) % 7 AS weekday,
               EXTRACT(hour FROM arrived_by AT TIME ZONE '{TZ}')::INT          AS hour,
               count(*)                                                        AS trips
        FROM trips_geo GROUP BY 1, 2
        """
    )
    grid = [[0] * 24 for _ in range(7)]
    for r in df.itertuples(index=False):
        grid[int(r.weekday)][int(r.hour)] = int(r.trips)
    return {"weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "hours": list(range(24)), "grid": grid}


@ttl_cache(settings.TTL_HISTORY)
def top_stations() -> dict:
    busiest = records(db.query_df(
        """
        WITH flow AS (
          SELECT origin_id AS sid FROM trips_geo
          UNION ALL SELECT destination_id FROM trips_geo
        )
        SELECT s.name, count(*) AS moves
        FROM flow JOIN stations s ON s.station_id = flow.sid
        GROUP BY 1 ORDER BY moves DESC LIMIT 12
        """
    ))
    od = records(db.query_df(
        """
        SELECT origin_name AS origin, dest_name AS destination, count(*) AS trips,
               round(avg(distance_km), 2) AS avg_km
        FROM trips_geo GROUP BY 1, 2 ORDER BY trips DESC LIMIT 12
        """
    ))
    return {"stations": busiest, "od_pairs": od}


# ---------- weather -------------------------------------------------------------

@ttl_cache(settings.TTL_HISTORY)
def weather_ridership() -> list[dict]:
    if not db.HAS_WEATHER:
        return []
    return records(db.query_df(
        f"""
        WITH d AS (
          SELECT CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) AS day, count(*) AS trips
          FROM trips_geo GROUP BY 1
        ),
        w AS (
          SELECT CAST(timestamp AT TIME ZONE '{TZ}' AS DATE) AS day,
                 round(avg(temperature), 1)  AS temp_c,
                 round(sum(precipitation), 1) AS precip_mm
          FROM weather GROUP BY 1
        )
        SELECT d.day::VARCHAR AS day, d.trips, w.temp_c, w.precip_mm
        FROM d JOIN w USING (day) ORDER BY d.day
        """
    ))


# ---------- per-station ---------------------------------------------------------

@ttl_cache(settings.TTL_STATION)
def station_detail(station_id: str) -> dict:
    meta = _one(db.query_df(
        """
        WITH latest AS (
          SELECT station_id,
                 arg_max(num_bikes_available, last_reported) AS bikes,
                 arg_max(num_docks_available, last_reported) AS docks,
                 arg_max(last_reported, last_reported)       AS as_of
          FROM observations WHERE station_id = ? GROUP BY 1
        )
        SELECT s.station_id, s.name, s.lat, s.lon,
               l.bikes, l.docks, (l.bikes + l.docks) AS capacity_now, l.as_of
        FROM stations s LEFT JOIN latest l ON l.station_id = s.station_id
        WHERE s.station_id = ?
        """,
        [station_id, station_id],
    ))
    if not meta:
        return {}
    if db.HAS_FORECAST:
        fc = _one(db.query_df(
            "SELECT p_empty, pred_bikes, t_target FROM forecast_live WHERE station_id = ?",
            [station_id],
        ))
        meta["forecast"] = fc or None
    typical = records(db.query_df(
        f"""
        SELECT EXTRACT(hour FROM last_reported AT TIME ZONE '{TZ}')::INT AS hour,
               round(avg(num_bikes_available), 2) AS avg_bikes
        FROM observations WHERE station_id = ? AND is_installed
        GROUP BY 1 ORDER BY 1
        """,
        [station_id],
    ))
    timeline = records(db.query_df(
        """
        SELECT last_reported AS ts, num_bikes_available AS bikes
        FROM observations
        WHERE station_id = ?
          AND last_reported >= (SELECT max(last_reported) FROM observations) - INTERVAL 3 DAY
        ORDER BY last_reported
        """,
        [station_id],
    ))
    meta["typical_day"] = typical
    meta["timeline"] = timeline
    return meta


# ---------- downloads (curated aggregates) --------------------------------------

@ttl_cache(settings.TTL_HISTORY)
def download_frame(name: str) -> pd.DataFrame:
    if name == "trips_daily":
        return db.query_df(
            f"""
            SELECT CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS day,
                   count(*) AS trips, count(DISTINCT bike_number) AS unique_bikes,
                   round(avg(distance_km), 3) AS avg_km, round(avg(duration_min), 1) AS avg_min
            FROM trips_geo GROUP BY 1 ORDER BY 1
            """
        )
    if name == "station_occupancy_daily":
        return db.query_df(
            f"""
            SELECT s.name AS station,
                   CAST(o.last_reported AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS day,
                   count(*) AS samples,
                   round(avg((o.num_bikes_available = 0)::INT) * 100, 1) AS pct_empty,
                   round(avg((o.num_docks_available = 0)::INT) * 100, 1) AS pct_full
            FROM observations o JOIN stations s ON s.station_id = o.station_id
            WHERE o.is_installed
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        )
    if name == "stations":
        return db.query_df(
            "SELECT station_id, name, lat, lon, capacity, address FROM stations ORDER BY name"
        )
    raise KeyError(name)


DOWNLOADS = ("trips_daily", "station_occupancy_daily", "stations")

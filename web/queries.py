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


def _exclude(alias: str = "s") -> tuple[str, list]:
    """SQL predicate + params to drop statically-excluded stations (depots etc.),
    e.g. ' AND s.name NOT IN (?)'. Empty when nothing is excluded."""
    names = settings.EXCLUDE_STATIONS
    if not names:
        return "", []
    placeholders = ", ".join(["?"] * len(names))
    return f" AND {alias}.name NOT IN ({placeholders})", list(names)


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
    xclause, xparams = _exclude("s")
    stations = _one(db.query_df(
        f"""
        WITH latest AS (
          SELECT station_id,
                 arg_max(num_bikes_available, last_reported) AS bikes,
                 arg_max(num_docks_available, last_reported) AS docks,
                 arg_max(is_installed, last_reported)        AS installed
          FROM observations GROUP BY 1
        )
        SELECT sum((l.bikes = 0 AND l.installed)::INT) AS stations_empty_now,
               sum((l.docks = 0 AND l.installed)::INT) AS stations_full_now,
               sum(l.installed::INT)                   AS stations_online
        FROM latest l JOIN stations s ON s.station_id = l.station_id
        WHERE TRUE{xclause}
        """,
        xparams,
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
        WHERE TRUE{xclause}
        GROUP BY 1 ORDER BY moves DESC LIMIT 1
        """,
        xparams,
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
    """CURRENT status per station (feeds the main map) — no forecast."""
    xclause, xparams = _exclude("s")
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
                    THEN round(l.bikes::DOUBLE / (l.bikes + l.docks), 3) END AS occ
        FROM stations s
        JOIN latest l ON l.station_id = s.station_id
        WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL{xclause}
        """,
        xparams,
    )
    return records(df)


@ttl_cache(settings.TTL_LIVE)
def _forecast_columns() -> list[str]:
    if not db.HAS_FORECAST:
        return []
    return list(db.query_df("SELECT * FROM forecast_live LIMIT 0").columns)


@ttl_cache(settings.TTL_LIVE)
def forecast_horizons() -> dict:
    """Available forecast horizons (min) + the base time the forecast was made from."""
    cols = _forecast_columns()
    if not cols:
        return {"available": False, "horizons": [], "generated_at": None}
    if "horizon_min" in cols:
        hz = db.query_df("SELECT DISTINCT horizon_min FROM forecast_live ORDER BY 1")
        horizons = [int(h) for h in hz["horizon_min"]]
    else:  # legacy single-horizon file
        horizons = []
    gen = records(db.query_df("SELECT max(t) AS t FROM forecast_live"))
    return {"available": True, "horizons": horizons,
            "generated_at": gen[0]["t"] if gen else None}


@ttl_cache(settings.TTL_LIVE)
def forecast_at(horizon_min: int) -> dict:
    """Per-station forecast at one horizon, joined to live coordinates + current bikes."""
    cols = _forecast_columns()
    if not cols:
        return {"available": False, "horizon_min": horizon_min, "t_target": None, "stations": []}
    where = "WHERE f.horizon_min = ? AND s.lat IS NOT NULL" if "horizon_min" in cols else "WHERE s.lat IS NOT NULL"
    params = [horizon_min] if "horizon_min" in cols else []
    xclause, xparams = _exclude("s")
    where += xclause
    params += xparams
    rows = records(db.query_df(
        f"""
        WITH latest AS (
          SELECT station_id, arg_max(num_bikes_available, last_reported) AS bikes_now
          FROM observations GROUP BY 1
        )
        SELECT f.station_id, s.name, s.lat, s.lon,
               f.t_target, f.pred_bikes, f.p_empty, f.capacity,
               COALESCE(l.bikes_now, f.bikes_now) AS bikes_now
        FROM forecast_live f
        JOIN stations s ON s.station_id = f.station_id
        LEFT JOIN latest l ON l.station_id = f.station_id
        {where}
        ORDER BY f.p_empty DESC
        """,
        params,
    ))
    return {"available": True, "horizon_min": horizon_min,
            "t_target": rows[0]["t_target"] if rows else None, "stations": rows}


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
    sx, sp = _exclude("s")
    busiest = records(db.query_df(
        f"""
        WITH flow AS (
          SELECT origin_id AS sid FROM trips_geo
          UNION ALL SELECT destination_id FROM trips_geo
        )
        SELECT s.name, count(*) AS moves
        FROM flow JOIN stations s ON s.station_id = flow.sid
        WHERE TRUE{sx}
        GROUP BY 1 ORDER BY moves DESC LIMIT 12
        """,
        sp,
    ))

    # Origin→destination pairs, with excluded stations (depots) dropped from both ends.
    names = settings.EXCLUDE_STATIONS
    if names:
        ph = ", ".join(["?"] * len(names))
        od_where = f"WHERE origin_name NOT IN ({ph}) AND dest_name NOT IN ({ph})"
        od_params = list(names) * 2
    else:
        od_where, od_params = "", []

    def od(order: str) -> list[dict]:
        return records(db.query_df(
            f"""
            SELECT origin_name AS origin, dest_name AS destination, count(*) AS trips,
                   round(avg(distance_km), 2) AS avg_km
            FROM trips_geo {od_where}
            GROUP BY 1, 2 ORDER BY trips {order}, 1, 2 LIMIT 5
            """,
            od_params,
        ))

    summary = _one(db.query_df(
        f"""
        SELECT count(*) AS distinct_pairs, COALESCE(sum((c = 1)::INT), 0) AS once_only FROM (
          SELECT count(*) AS c FROM trips_geo {od_where} GROUP BY origin_name, dest_name
        )
        """,
        od_params,
    ))
    return {"stations": busiest, "od_most": od("DESC"), "od_least": od("ASC"),
            "od_distinct": summary.get("distinct_pairs"), "od_once": summary.get("once_only")}


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
    # Current status only — forecasts are served per-horizon by /api/forecast.
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

"""All SQL lives here. Each public function is TTL-cached and returns
JSON-native structures (via `records()`), so route handlers stay trivial.

Everything reads the small tables materialized in db.py (`trips_geo`,
`obs_latest`, `bike_last`, …) rather than the Parquet-backed views — a full view
scan costs seconds, the tables ~milliseconds. Only `station_detail`'s timeline
touches raw observations, partition-pruned to the last few days. SQL mirrors
analysis/stats.py so the site and the offline reports agree on definitions.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

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
        f"SELECT CAST(max(as_of) AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS d FROM obs_latest"
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
        """
        SELECT (SELECT max(as_of) FROM obs_latest) AS last_observation,
               m.first_observation, m.stations
        FROM obs_meta m
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
        WITH now AS (SELECT max(last_seen) AS t FROM bike_last)
        SELECT (SELECT count(*) FROM bike_last, now
                WHERE last_seen >= now.t - INTERVAL 24 HOUR) AS fleet_recent,
               (SELECT count(*) FROM bike_last, now WHERE last_seen = now.t) AS visible_now
        """
    ))
    xclause, xparams = _exclude("s")
    stations = _one(db.query_df(
        f"""
        SELECT sum((l.bikes = 0 AND l.installed)::INT) AS stations_empty_now,
               sum((l.docks = 0 AND l.installed)::INT) AS stations_full_now,
               sum(l.installed::INT)                   AS stations_online,
               sum(CASE WHEN l.installed THEN l.bikes ELSE 0 END) AS available_now,
               sum(CASE WHEN l.installed THEN l.docks ELSE 0 END) AS docks_free_now
        FROM obs_latest l JOIN stations s ON s.station_id = l.station_id
        WHERE TRUE{xclause}
        """,
        xparams,
    ))
    most_bikes = _one(db.query_df(
        f"""
        SELECT s.name AS station, l.bikes AS bikes
        FROM obs_latest l JOIN stations s ON s.station_id = l.station_id
        WHERE l.installed{xclause}
        ORDER BY l.bikes DESC, s.name LIMIT 1
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
        SELECT s.name AS station, count(*) AS moves
        FROM flow JOIN stations s ON s.station_id = flow.sid
        WHERE TRUE{xclause}
        GROUP BY 1 ORDER BY moves DESC LIMIT 1
        """,
        xparams,
    ))
    top_bike = _one(db.query_df(
        f"""
        SELECT bike_number, count(*) AS trips
        FROM trips_geo
        WHERE CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) = DATE '{day}'
        GROUP BY 1 ORDER BY trips DESC, bike_number LIMIT 1
        """
    ))
    peak = _one(db.query_df(
        f"""
        SELECT EXTRACT(hour FROM arrived_by AT TIME ZONE '{TZ}')::INT AS hour, count(*) AS trips
        FROM trips_geo
        WHERE CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) = DATE '{day}'
        GROUP BY 1 ORDER BY trips DESC, hour LIMIT 1
        """
    ))
    # Longest actual ride today (by straight-line km) + its route. Drop implausible
    # speeds (>30 km/h) so a rebalancing truck-move can't pose as the longest ride.
    names = settings.EXCLUDE_STATIONS
    odw, odp = "", []
    if names:
        ph = ", ".join(["?"] * len(names))
        odw = f"AND origin_name NOT IN ({ph}) AND dest_name NOT IN ({ph})"
        odp = list(names) * 2
    longest = _one(db.query_df(
        f"""
        SELECT round(distance_km, 1) AS longest_km, origin_name, dest_name,
               round(duration_min, 0) AS longest_min
        FROM trips_geo
        WHERE CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE) = DATE '{day}'
          AND duration_min > 0 AND distance_km / (duration_min / 60.0) <= 30 {odw}
        ORDER BY distance_km DESC LIMIT 1
        """,
        odp,
    ))
    bikes_in_use = max(int(fleet.get("fleet_recent") or 0) - int(fleet.get("visible_now") or 0), 0)
    return {
        "day": day,
        **trips,
        "bikes_in_use": bikes_in_use,
        **stations,
        "longest_km": longest.get("longest_km"),
        "longest_from": longest.get("origin_name"),
        "longest_to": longest.get("dest_name"),
        "longest_min": longest.get("longest_min"),
        "busiest_station": busiest.get("station"),
        "busiest_moves": busiest.get("moves"),
        "most_bikes": most_bikes.get("bikes"),
        "most_bikes_station": most_bikes.get("station"),
        "top_bike_number": top_bike.get("bike_number"),
        "top_bike_trips": top_bike.get("trips"),
        "peak_hour": peak.get("hour"),
        "peak_hour_trips": peak.get("trips"),
    }


@ttl_cache(settings.TTL_LIVE)
def stations_live() -> list[dict]:
    """CURRENT status per station (feeds the main map) — no forecast."""
    xclause, xparams = _exclude("s")
    df = db.query_df(
        f"""
        SELECT s.station_id, s.name, s.lat, s.lon,
               l.bikes, l.docks, l.installed,
               (l.bikes + l.docks) AS capacity_now,
               CASE WHEN (l.bikes + l.docks) > 0
                    THEN round(l.bikes::DOUBLE / (l.bikes + l.docks), 3) END AS occ
        FROM stations s
        JOIN obs_latest l ON l.station_id = s.station_id
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
        SELECT f.station_id, s.name, s.lat, s.lon,
               f.t_target, f.pred_bikes, f.p_empty, f.capacity,
               COALESCE(l.bikes, f.bikes_now) AS bikes_now
        FROM forecast_live f
        JOIN stations s ON s.station_id = f.station_id
        LEFT JOIN obs_latest l ON l.station_id = f.station_id
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
               (SELECT count(*) FROM bike_last)                    AS unique_bikes,
               (SELECT count(*) FROM stations)                     AS stations,
               (SELECT min(first_seen) FROM bike_last)             AS first_seen,
               (SELECT max(last_seen) FROM bike_last)              AS last_seen
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

    od_most = records(db.query_df(
        f"""
        SELECT origin_name AS origin, dest_name AS destination, count(*) AS trips,
               round(avg(distance_km), 2) AS avg_km
        FROM trips_geo {od_where}
        GROUP BY 1, 2 ORDER BY trips DESC, origin_name, dest_name LIMIT 5
        """,
        od_params,
    ))

    # "Be the first one": directed station pairs nobody has ridden yet. Depot and
    # coordinate-less stations excluded from both ends.
    onx, onp = "", []
    if names:
        ph = ", ".join(["?"] * len(names))
        onx = f"AND o.name NOT IN ({ph}) AND d.name NOT IN ({ph})"
        onp = list(names) * 2
    never_sql = f"""
        WITH ridden AS (SELECT DISTINCT origin_id AS o, destination_id AS d FROM trips_geo)
        SELECT o.name AS origin, d.name AS destination
        FROM stations o CROSS JOIN stations d
        WHERE o.station_id <> d.station_id
          AND o.lat IS NOT NULL AND d.lat IS NOT NULL {onx}
          AND NOT EXISTS (SELECT 1 FROM ridden r WHERE r.o = o.station_id AND r.d = d.station_id)
    """
    never = records(db.query_df(
        never_sql + " ORDER BY hash(o.name || '|' || d.name) LIMIT 5", onp))
    never_count = _one(db.query_df(
        f"SELECT count(*) AS n FROM ({never_sql})", onp)).get("n")

    return {"stations": busiest, "od_most": od_most,
            "od_never": never, "od_never_count": never_count}


@ttl_cache(settings.TTL_HISTORY)
def bike_usage() -> dict:
    """Most-ridden bikes, plus 'forgotten' bikes — ones with zero rides ever (never
    changed station), where they've sat and for how long."""
    most = records(db.query_df(
        """
        WITH top AS (
          SELECT bike_number, count(*) AS trips, round(sum(distance_km), 1) AS km
          FROM trips_geo GROUP BY 1 ORDER BY trips DESC, km DESC LIMIT 5
        )
        SELECT t.bike_number, t.trips, t.km, s.name AS last_station
        FROM top t
        LEFT JOIN bike_last bl USING (bike_number)   -- last station each was parked at
        LEFT JOIN stations s ON s.station_id = bl.station_id
        ORDER BY t.trips DESC, t.km DESC
        """
    ))
    # "Forgotten" = bikes that HAVE been ridden (≥1 trip) and are STILL in the
    # fleet (seen in the last 2 days, so not removed) but have sat unused the
    # longest — ranked by days since their last ride. Never-moved bikes are
    # excluded, as are bikes sitting in the depot.
    sx, sp = "", []
    if settings.EXCLUDE_STATIONS:
        ph = ", ".join(["?"] * len(settings.EXCLUDE_STATIONS))
        sx = f"AND st.name NOT IN ({ph})"
        sp = list(settings.EXCLUDE_STATIONS)
    idle = db.query_df(
        f"""
        WITH now AS (SELECT max(last_seen) AS t FROM bike_last),
        lasttrip AS (SELECT bike_number, max(arrived_by) AS last_trip FROM trips_geo GROUP BY 1)
        SELECT bl.bike_number, st.name AS station,
               date_diff('day', lt.last_trip, (SELECT t FROM now)) AS days_idle
        FROM bike_last bl
        JOIN lasttrip lt USING (bike_number)                          -- moved at least once
        JOIN stations st ON st.station_id = bl.station_id
        WHERE bl.last_docked >= (SELECT t FROM now) - INTERVAL 2 DAY  -- still in the fleet
          {sx}
        ORDER BY days_idle DESC, hash(bl.bike_number)
        """,
        sp,
    )
    forgotten = records(idle.head(5))
    active_count = int(len(idle))
    idle_count = int((idle["days_idle"] >= 7).sum()) if len(idle) else 0
    return {"most": most, "forgotten": forgotten,
            "idle_count": idle_count, "active_count": active_count}


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
        SELECT s.station_id, s.name, s.lat, s.lon,
               l.bikes, l.docks, (l.bikes + l.docks) AS capacity_now, l.as_of
        FROM stations s LEFT JOIN obs_latest l ON l.station_id = s.station_id
        WHERE s.station_id = ?
        """,
        [station_id],
    ))
    if not meta:
        return {}
    # Current status only — forecasts are served per-horizon by /api/forecast.
    typical = records(db.query_df(
        "SELECT hour, avg_bikes FROM station_hourly WHERE station_id = ? ORDER BY hour",
        [station_id],
    ))
    # Raw counts, last 3 days. The literal date predicate prunes the partition
    # glob to a handful of files; the timestamp filter does the exact cut.
    date_clause = ""
    if (latest := db.max_obs_date()) is not None:
        lo = (date.fromisoformat(latest) - timedelta(days=4)).isoformat()
        date_clause = f" AND date >= DATE '{lo}'"
    timeline = records(db.query_df(
        f"""
        SELECT last_reported AS ts, num_bikes_available AS bikes
        FROM observations
        WHERE station_id = ?{date_clause}
          AND last_reported >= (SELECT max(as_of) FROM obs_latest) - INTERVAL 3 DAY
        ORDER BY last_reported
        """,
        [station_id],
    ))
    meta["typical_day"] = typical
    meta["timeline"] = timeline
    return meta


@ttl_cache(settings.TTL_STATION)
def station_insights(station_id: str) -> dict:
    """Trip-derived stats for one station (feeds the /station/<id> page): when
    people ride here, where they go, and superlatives. Reads only the in-memory
    `trips_geo` table, so it stays fast without warming."""
    meta = _one(db.query_df(
        "SELECT station_id, name FROM stations WHERE station_id = ?", [station_id]))
    if not meta:
        return {}

    counts = _one(db.query_df(
        """
        SELECT (SELECT count(*) FROM trips_geo WHERE origin_id = ?)      AS departures,
               (SELECT count(*) FROM trips_geo WHERE destination_id = ?) AS arrivals,
               (SELECT round(avg(duration_min), 1) FROM trips_geo WHERE origin_id = ?) AS avg_min,
               (SELECT round(avg(distance_km), 2)  FROM trips_geo WHERE origin_id = ?) AS avg_km
        """,
        [station_id] * 4,
    ))

    names = settings.EXCLUDE_STATIONS
    dx, dp = "", []
    if names:
        ph = ", ".join(["?"] * len(names))
        dx = f" AND dest_name NOT IN ({ph})"
        dp = list(names)

    # Longest actual ride from here — same implausible-speed filter as
    # stats_today, so a rebalancing truck-move can't pose as the longest ride.
    longest = _one(db.query_df(
        f"""
        SELECT round(distance_km, 1) AS longest_km, dest_name AS longest_to,
               round(duration_min, 0) AS longest_min,
               CAST(arrived_by AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS longest_day
        FROM trips_geo
        WHERE origin_id = ? AND duration_min > 0
          AND distance_km / (duration_min / 60.0) <= 30{dx}
        ORDER BY distance_km DESC LIMIT 1
        """,
        [station_id, *dp],
    ))

    top_dest = records(db.query_df(
        f"""
        SELECT dest_name AS name, count(*) AS trips, round(avg(distance_km), 2) AS avg_km
        FROM trips_geo
        WHERE origin_id = ?{dx}
        GROUP BY 1 ORDER BY trips DESC, name LIMIT 5
        """,
        [station_id, *dp],
    ))

    # Weekday × hour grid of moves: departures counted at their departure time,
    # arrivals at their arrival time (each endpoint is ±5 min, see _trips.py).
    moves_sql = """
        SELECT departed_after AS t FROM trips_geo WHERE origin_id = ?
        UNION ALL
        SELECT arrived_by FROM trips_geo WHERE destination_id = ?
    """
    moves = db.query_df(
        f"""
        SELECT (EXTRACT(dow FROM t AT TIME ZONE '{TZ}')::INT + 6) % 7 AS weekday,
               EXTRACT(hour FROM t AT TIME ZONE '{TZ}')::INT          AS hour,
               count(*)                                               AS moves
        FROM ({moves_sql}) GROUP BY 1, 2
        """,
        [station_id, station_id],
    )
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    grid = [[0] * 24 for _ in range(7)]
    for r in moves.itertuples(index=False):
        grid[int(r.weekday)][int(r.hour)] = int(r.moves)
    peak_weekday = peak_hour = peak_moves = None
    for wd in range(7):
        for h in range(24):
            if grid[wd][h] and (peak_moves is None or grid[wd][h] > peak_moves):
                peak_weekday, peak_hour, peak_moves = weekdays[wd], h, grid[wd][h]

    busiest = _one(db.query_df(
        f"""
        SELECT CAST(t AT TIME ZONE '{TZ}' AS DATE)::VARCHAR AS busiest_day,
               count(*) AS busiest_moves
        FROM ({moves_sql}) GROUP BY 1 ORDER BY busiest_moves DESC, busiest_day LIMIT 1
        """,
        [station_id, station_id],
    ))

    return {
        **meta, **counts, **longest, **busiest,
        "top_destinations": top_dest,
        "peak_weekday": peak_weekday, "peak_hour": peak_hour, "peak_moves": peak_moves,
        "heatmap": {"weekdays": weekdays, "hours": list(range(24)), "grid": grid},
    }


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


# ---------- cache warming --------------------------------------------------------
# Called by the app's background refresher right after the underlying tables are
# rebuilt: drop the stale entries, then recompute, so requests always hit memory
# and never see pre-refresh data. (station_detail/forecast_at are per-key and
# cheap now — they stay plain TTL.)

_LIVE_FNS = (latest_day, health, _forecast_columns, forecast_horizons,
             stats_today, stations_live)
_HISTORY_FNS = (history_summary, trips_daily, heatmap, top_stations,
                bike_usage, weather_ridership)


def _rewarm(fns) -> None:
    for fn in fns:
        fn.cache_clear()
    for fn in fns:
        fn()


def warm_live() -> None:
    """Recompute the live-TTL caches (after each obs_latest refresh)."""
    _rewarm(_LIVE_FNS)


def warm_history() -> None:
    """Recompute the trip-derived caches (after each full trips rebuild)."""
    _rewarm(_HISTORY_FNS)

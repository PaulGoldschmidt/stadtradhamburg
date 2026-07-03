"""Fetch hourly Hamburg weather from Bright Sky (DWD) into a Parquet store.

Feeds the bike-utilization forecasting pipeline: the `weather` view this writes
is LEFT JOINed by hour onto the resampled station grid. Source is Deutscher
Wetterdienst via the free, key-less Bright Sky API (https://brightsky.dev);
lat/lon snaps to the nearest station — Hamburg-Fuhlsbüttel (DWD 01975) for the
city center.

Idempotent, like the pollers: only hours missing from the store are fetched, so
re-running tops up the backlog. Past hours come back as DWD *observations*;
future hours (if requested) come back as DWD MOSMIX *forecasts* from the same
endpoint. Atomic writes (tmp + os.replace), zstd, one record per hour (UTC).

By default the window is auto-detected from the bike observations so weather
lines up exactly with the data we have.

Usage:
    .venv/bin/python analysis/weather.py                      # cover the bike-data window
    .venv/bin/python analysis/weather.py --start 2026-05-29 --end 2026-06-28
    .venv/bin/python analysis/weather.py --force              # refetch all (upgrades forecasts → obs)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Stay importable both as `python analysis/weather.py` and `python -m analysis.weather`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _trips import data_root  # noqa: E402

LOG = logging.getLogger("weather")

BRIGHTSKY_URL = "https://api.brightsky.dev/weather"
# Hamburg city center; Bright Sky snaps to the nearest DWD station (Fuhlsbüttel).
HAMBURG_LAT = 53.55
HAMBURG_LON = 10.00
HTTP_TIMEOUT = 30
USER_AGENT = "stadtradhamburg-weather/0.1 (+https://github.com/PaulGoldschmidt/stadtradhamburg)"

# Numeric weather vars are float64 so a station's missing report stays null
# rather than forcing a sentinel. Units (Bright Sky defaults): temperature °C,
# precipitation mm, wind km/h, pressure hPa, humidity/cloud %, sunshine min.
WEATHER_SCHEMA = pa.schema(
    [
        ("timestamp", pa.timestamp("ns", tz="UTC")),  # the hour, UTC
        ("temperature", pa.float64()),
        ("precipitation", pa.float64()),
        ("wind_speed", pa.float64()),
        ("wind_gust_speed", pa.float64()),
        ("wind_direction", pa.float64()),
        ("relative_humidity", pa.float64()),
        ("cloud_cover", pa.float64()),
        ("sunshine", pa.float64()),
        ("pressure_msl", pa.float64()),
        ("visibility", pa.float64()),
        ("dew_point", pa.float64()),
        ("solar", pa.float64()),
        ("condition", pa.string()),
        ("source_dwd_station_id", pa.string()),
        ("is_forecast", pa.bool_()),
        ("fetched_at", pa.timestamp("ns", tz="UTC")),
    ]
)

NUMERIC_FIELDS = [
    "temperature", "precipitation", "wind_speed", "wind_gust_speed",
    "wind_direction", "relative_humidity", "cloud_cover", "sunshine",
    "pressure_msl", "visibility", "dew_point", "solar",
]


# ---------- HTTP ----------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


# ---------- Store ---------------------------------------------------------------

def weather_path(data_dir: Path) -> Path:
    return data_dir / "weather" / "hourly.parquet"


def build_weather_view(con: duckdb.DuckDBPyConnection, path: Path | None = None) -> Path:
    """Expose the store as a DuckDB `weather` view for the forecasting pipeline."""
    p = path or weather_path(data_root())
    con.execute(f"CREATE OR REPLACE VIEW weather AS SELECT * FROM '{p}'")
    return p


def load_store(path: Path) -> dict[datetime, dict[str, Any]]:
    """Existing records keyed by their (UTC, on-the-hour) timestamp."""
    if not path.exists():
        return {}
    return {r["timestamp"]: r for r in pq.read_table(path).to_pylist()}


def write_store(path: Path, store: dict[datetime, dict[str, Any]]) -> None:
    records = [store[k] for k in sorted(store)]
    table = pa.Table.from_pylist(records, schema=WEATHER_SCHEMA)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


# ---------- Window detection ----------------------------------------------------

def floor_hour(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def detect_window(data_dir: Path) -> tuple[datetime, datetime]:
    """Min/max observation time across the bike data, as UTC hour bounds."""
    con = duckdb.connect()
    glob = data_dir / "observations" / "date=*" / "part-*.parquet"
    lo, hi = con.execute(
        f"SELECT min(last_reported), max(last_reported) FROM '{glob}'"
    ).fetchone()
    if lo is None:
        sys.exit(f"no observations found under {glob}")
    return floor_hour(lo), floor_hour(hi) + timedelta(hours=1)


def hour_range(start: datetime, end: datetime) -> list[datetime]:
    """Inclusive list of on-the-hour UTC datetimes from start to end."""
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(hours=1)
    return out


# ---------- Fetch ---------------------------------------------------------------

def parse_ts(s: str) -> datetime:
    return floor_hour(datetime.fromisoformat(s))


def fetch_range(
    session: requests.Session,
    lat: float,
    lon: float,
    start_date: datetime,
    end_date: datetime,
) -> list[dict[str, Any]]:
    """Fetch [start_date, end_date] (UTC dates). Returns raw Bright Sky hour dicts
    annotated with resolved DWD station id and observed/forecast flag."""
    r = session.get(
        BRIGHTSKY_URL,
        params={
            "lat": lat,
            "lon": lon,
            "date": start_date.date().isoformat(),
            "last_date": end_date.date().isoformat(),
            "tz": "UTC",
        },
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    sources_by_id = {s["id"]: s for s in body.get("sources", [])}
    rows = []
    for w in body.get("weather", []):
        src = sources_by_id.get(w.get("source_id"), {})
        rows.append({**w, "_dwd_station_id": src.get("dwd_station_id"),
                     "_is_forecast": (src.get("observation_type") == "forecast")})
    return rows


def to_record(w: dict[str, Any], now: datetime) -> dict[str, Any]:
    rec: dict[str, Any] = {"timestamp": parse_ts(w["timestamp"])}
    for f in NUMERIC_FIELDS:
        v = w.get(f)
        rec[f] = float(v) if v is not None else None
    rec["condition"] = w.get("condition")
    rec["source_dwd_station_id"] = w.get("_dwd_station_id")
    rec["is_forecast"] = bool(w.get("_is_forecast"))
    rec["fetched_at"] = now
    return rec


def fetch_missing(
    session: requests.Session,
    lat: float,
    lon: float,
    missing: list[datetime],
    chunk_days: int,
) -> list[dict[str, Any]]:
    """Fetch the span covering `missing` in chunks, keep only the missing hours."""
    want = set(missing)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Bright Sky's `last_date` is inclusive only of that day's 00:00 hour, so
    # chunks must OVERLAP at the boundary (next chunk re-requests b's midnight)
    # rather than skip to b+1 — otherwise hours 01:00–23:00 of each boundary day
    # are silently dropped. The `want` filter discards the duplicated hour.
    span_start = missing[0].replace(hour=0)
    span_end_excl = missing[-1].replace(hour=0) + timedelta(days=1)

    out: list[dict[str, Any]] = []
    a = span_start
    while a < span_end_excl:
        b = min(a + timedelta(days=chunk_days), span_end_excl)
        LOG.info("fetching %s … %s", a.date(), b.date())
        for w in fetch_range(session, lat, lon, a, b):
            ts = parse_ts(w["timestamp"])
            if ts in want:
                out.append(to_record(w, now))
        a = b
    return out


# ---------- Main ----------------------------------------------------------------

def run(
    data_dir: Path,
    lat: float,
    lon: float,
    start: datetime | None,
    end: datetime | None,
    force: bool,
    chunk_days: int,
) -> int:
    if start is None or end is None:
        start, end = detect_window(data_dir)
    LOG.info("target window: %s … %s (UTC, hourly)", start, end)

    path = weather_path(data_dir)
    store = {} if force else load_store(path)
    desired = hour_range(start, end)
    missing = sorted(set(desired) - set(store))
    if not missing:
        LOG.info("store already covers all %d hours — nothing to fetch", len(desired))
        return 0
    LOG.info("%d of %d hours present; fetching %d missing",
             len(desired) - len(missing), len(desired), len(missing))

    session = make_session()
    new_records = fetch_missing(session, lat, lon, missing, chunk_days)
    if not new_records:
        LOG.warning("API returned no data for the missing hours (future/unavailable?)")
        return 0

    for rec in new_records:
        store[rec["timestamp"]] = rec  # newer fetch wins (forecast → observed upgrade)
    write_store(path, store)

    forecasts = sum(1 for r in new_records if r["is_forecast"])
    still_missing = len(set(desired) - set(store))
    LOG.info(
        "wrote %d new hours (%d forecast) → %s; store now %d hours (%s … %s), %d still missing",
        len(new_records), forecasts, path, len(store),
        min(store).date(), max(store).date(), still_missing,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=None,
                   help="Data root (default: autodetect via _trips.data_root()).")
    p.add_argument("--start", help="Window start YYYY-MM-DD (UTC). Default: from bike data.")
    p.add_argument("--end", help="Window end YYYY-MM-DD (UTC, inclusive). Default: from bike data.")
    p.add_argument("--lat", type=float, default=HAMBURG_LAT)
    p.add_argument("--lon", type=float, default=HAMBURG_LON)
    p.add_argument("--force", action="store_true",
                   help="Refetch the whole window, ignoring the existing store.")
    p.add_argument("--chunk-days", type=int, default=7,
                   help="Days per Bright Sky request (default 7).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    data_dir = args.data_dir or data_root()

    def as_utc_day(s: str | None) -> datetime | None:
        return (datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if s else None)

    start = as_utc_day(args.start)
    end = as_utc_day(args.end)
    if end is not None:
        end += timedelta(hours=23)  # make --end inclusive of its whole day

    return run(data_dir, args.lat, args.lon, start, end, args.force, args.chunk_days)


if __name__ == "__main__":
    sys.exit(main())

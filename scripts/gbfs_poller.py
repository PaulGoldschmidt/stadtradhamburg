"""Poll Deutsche Bahn's Shared-Mobility GBFS API for StadtRad Hamburg and append
new station snapshots to a partitioned Parquet store.

Live endpoints:
    https://apis.deutschebahn.com/db-api-marketplace/apis/shared-mobility-gbfs/v2/de
        /StadtRadHamburg/station_information
        /StadtRadHamburg/station_status

Auth is header-based: DB-Client-ID + DB-Api-Key, loaded from a .env file at the
repo root (clientid / clientsecret keys).

Layout produced inside --data-dir (default: <repo>/data/gbfs):
    observations/date=YYYY-MM-DD/part-YYYYMMDDTHHMMSSZ.parquet
    stations.parquet
    state.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("gbfs")

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

BASE_URL = (
    "https://apis.deutschebahn.com/db-api-marketplace/apis"
    "/shared-mobility-gbfs/v2/de/StadtRadHamburg"
)
STATION_INFO_URL = f"{BASE_URL}/station_information"
STATION_STATUS_URL = f"{BASE_URL}/station_status"
FREE_BIKE_STATUS_URL = f"{BASE_URL}/free_bike_status"

POLL_INTERVAL_DEFAULT = 300
METADATA_REFRESH_SECONDS = 86400
HTTP_TIMEOUT = 30
USER_AGENT = "stadtradhamburg-poller/0.1 (+https://github.com/PaulGoldschmidt/stadtradhamburg)"

OBS_SCHEMA = pa.schema(
    [
        ("station_id", pa.string()),
        ("last_reported", pa.timestamp("ns", tz="UTC")),
        ("num_bikes_available", pa.int32()),
        ("num_docks_available", pa.int32()),
        ("is_installed", pa.bool_()),
        ("is_renting", pa.bool_()),
        ("is_returning", pa.bool_()),
        ("ingested_at", pa.timestamp("ns", tz="UTC")),
    ]
)

STATIONS_SCHEMA = pa.schema(
    [
        ("station_id", pa.string()),
        ("name", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("capacity", pa.int32()),
        ("address", pa.string()),
        ("refreshed_at", pa.timestamp("ns", tz="UTC")),
    ]
)

BIKES_SCHEMA = pa.schema(
    [
        ("bike_number", pa.string()),
        ("bike_id", pa.string()),
        ("station_id", pa.string()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("is_reserved", pa.bool_()),
        ("is_disabled", pa.bool_()),
        ("vehicle_type_id", pa.string()),
        ("observed_at", pa.timestamp("ns", tz="UTC")),
        ("ingested_at", pa.timestamp("ns", tz="UTC")),
    ]
)


# ---------- HTTP ----------------------------------------------------------------

def make_session() -> requests.Session:
    client_id = os.environ.get("clientid")
    client_secret = os.environ.get("clientsecret")
    if not client_id or not client_secret:
        raise SystemExit(
            "Missing DB API credentials: set 'clientid' and 'clientsecret' "
            f"in {REPO_ROOT / '.env'} or the environment."
        )
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "DB-Client-ID": client_id,
            "DB-Api-Key": client_secret,
        }
    )
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


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------- State ---------------------------------------------------------------

def load_state(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, int]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=2))
    os.replace(tmp, path)


# ---------- Parquet writers -----------------------------------------------------

def write_observations(rows: list[dict[str, Any]], data_dir: Path) -> int:
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        day = r["last_reported"].astimezone(timezone.utc).date().isoformat()
        by_date.setdefault(day, []).append(r)

    total = 0
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for day, day_rows in by_date.items():
        part_dir = data_dir / "observations" / f"date={day}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / f"part-{stamp}.parquet"
        tmp = out.with_suffix(".parquet.tmp")
        table = pa.Table.from_pylist(
            [{**r, "ingested_at": now} for r in day_rows],
            schema=OBS_SCHEMA,
        )
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, out)
        total += len(day_rows)
        LOG.info("wrote %d obs → %s", len(day_rows), out)
    return total


def write_stations(items: list[dict[str, Any]], data_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    records = []
    for it in items:
        capacity = it.get("capacity")
        records.append(
            {
                "station_id": str(it["station_id"]),
                "name": it.get("name"),
                "lat": float(it["lat"]) if it.get("lat") is not None else None,
                "lon": float(it["lon"]) if it.get("lon") is not None else None,
                "capacity": int(capacity) if capacity is not None else None,
                "address": it.get("address"),
                "refreshed_at": now,
            }
        )
    table = pa.Table.from_pylist(records, schema=STATIONS_SCHEMA)
    out = data_dir / "stations.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, out)
    LOG.info("wrote %d stations → %s", len(records), out)


def write_bikes(rows: list[dict[str, Any]], data_dir: Path) -> int:
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    day = rows[0]["observed_at"].astimezone(timezone.utc).date().isoformat()
    part_dir = data_dir / "bikes" / f"date={day}"
    part_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = part_dir / f"part-{stamp}.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    table = pa.Table.from_pylist(
        [{**r, "ingested_at": now} for r in rows],
        schema=BIKES_SCHEMA,
    )
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, out)
    LOG.info("wrote %d bikes → %s", len(rows), out)
    return len(rows)


# ---------- Core polling --------------------------------------------------------

def posix_to_dt(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def fetch_and_write_bikes(
    session: requests.Session, data_dir: Path
) -> tuple[int, int]:
    """Fetch free_bike_status and append a snapshot. Returns (bikes_seen, rows_written)."""
    payload = get_json(session, FREE_BIKE_STATUS_URL)
    bikes = (payload.get("data") or {}).get("bikes") or []
    observed_at = posix_to_dt(payload["last_updated"])
    rows = []
    for b in bikes:
        bike_number = b.get("_bike_number")
        if bike_number is None:
            continue
        rows.append(
            {
                "bike_number": str(bike_number),
                "bike_id": str(b["bike_id"]) if b.get("bike_id") is not None else None,
                "station_id": (
                    str(b["station_id"]) if b.get("station_id") is not None else None
                ),
                "lat": float(b["lat"]) if b.get("lat") is not None else None,
                "lon": float(b["lon"]) if b.get("lon") is not None else None,
                "is_reserved": bool(b.get("is_reserved")),
                "is_disabled": bool(b.get("is_disabled")),
                "vehicle_type_id": b.get("vehicle_type_id"),
                "observed_at": observed_at,
            }
        )
    written = write_bikes(rows, data_dir)
    return len(bikes), written


def poll_once(
    session: requests.Session, state: dict[str, int], data_dir: Path
) -> tuple[int, int, int, int]:
    """Returns (stations_seen, observation_rows_written, bikes_seen, bike_rows_written)."""
    payload = get_json(session, STATION_STATUS_URL)
    ttl = payload.get("ttl")
    if isinstance(ttl, int) and ttl > POLL_INTERVAL_DEFAULT:
        LOG.debug("server ttl=%ds exceeds default interval", ttl)

    stations = (payload.get("data") or {}).get("stations") or []
    first_run = not state
    new_rows: list[dict[str, Any]] = []

    for s in stations:
        sid = str(s.get("station_id"))
        last_reported_raw = s.get("last_reported")
        if last_reported_raw is None:
            continue
        last_reported_ts = int(last_reported_raw)

        prev = state.get(sid)
        if prev is not None and last_reported_ts <= prev:
            continue

        state[sid] = last_reported_ts
        if first_run:
            continue

        new_rows.append(
            {
                "station_id": sid,
                "last_reported": posix_to_dt(last_reported_ts),
                "num_bikes_available": int(s["num_bikes_available"]),
                "num_docks_available": (
                    int(s["num_docks_available"])
                    if s.get("num_docks_available") is not None
                    else None
                ),
                "is_installed": bool(s.get("is_installed")),
                "is_renting": bool(s.get("is_renting")),
                "is_returning": bool(s.get("is_returning")),
            }
        )

    if first_run:
        LOG.info(
            "first run: seeded cursors for %d stations (no obs written)",
            len(stations),
        )
        written = 0
    else:
        written = write_observations(new_rows, data_dir)

    bikes_seen, bike_rows = fetch_and_write_bikes(session, data_dir)

    return len(stations), written, bikes_seen, bike_rows


def refresh_metadata(session: requests.Session, data_dir: Path) -> None:
    payload = get_json(session, STATION_INFO_URL)
    stations = (payload.get("data") or {}).get("stations") or []
    write_stations(stations, data_dir)


# ---------- Daemon loop ---------------------------------------------------------

_stop = False


def _install_signal_handlers() -> None:
    def handle(signum, _frame):
        global _stop
        LOG.info("received signal %d, will exit after current cycle", signum)
        _stop = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def run(data_dir: Path, interval: int, once: bool) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "state.json"
    session = make_session()

    last_metadata = 0.0
    while True:
        cycle_start = time.monotonic()
        try:
            if time.time() - last_metadata > METADATA_REFRESH_SECONDS:
                refresh_metadata(session, data_dir)
                last_metadata = time.time()

            state = load_state(state_path)
            seen, written, bikes_seen, bike_rows = poll_once(session, state, data_dir)
            save_state(state_path, state)
            LOG.info(
                "cycle done: %d stations seen, %d obs rows, %d bikes seen, %d bike rows",
                seen,
                written,
                bikes_seen,
                bike_rows,
            )
        except Exception:
            LOG.exception("poll cycle failed")

        if once or _stop:
            return 0

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1.0, interval - elapsed)
        slept = 0.0
        while slept < sleep_for and not _stop:
            chunk = min(1.0, sleep_for - slept)
            time.sleep(chunk)
            slept += chunk
        if _stop:
            return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data" / "gbfs",
        help="Output directory (default: repo /data/gbfs).",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_DEFAULT,
        help="Seconds between polls (default: 300).",
    )
    p.add_argument("--once", action="store_true", help="Run a single cycle and exit.")
    p.add_argument(
        "--log-level", default="INFO", help="DEBUG/INFO/WARNING/ERROR (default INFO)."
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _install_signal_handlers()
    return run(args.data_dir, args.interval, args.once)


if __name__ == "__main__":
    sys.exit(main())

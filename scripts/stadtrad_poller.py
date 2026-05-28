"""Poll Hamburg's StadtRad SensorThings API and append new observations to a
partitioned Parquet store.

Live endpoint:
    https://iot.hamburg.de/v1.0/Datastreams
    ?$filter=((properties/serviceName eq 'HH_STA_StadtRad')
             and (properties/layerName eq 'Fahrraeder'))
    &$expand=Observations($top=10;$orderby=phenomenonTime desc;
                          $select=phenomenonTime,result)
    &$count=true

The archive snapshot under data/datastreams_173439_snap_25.json is frozen and
is only kept for reference.

Layout produced inside --data-dir:
    observations/date=YYYY-MM-DD/part-YYYYMMDDTHHMMSSZ.parquet
    datastreams.parquet
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
from typing import Any, Iterator
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("stadtrad")

BASE_URL = "https://iot.hamburg.de/v1.0/Datastreams"
FILTER = (
    "((properties/serviceName eq 'HH_STA_StadtRad') "
    "and (properties/layerName eq 'Fahrraeder'))"
)
EXPAND_OBS = (
    "Observations("
    "$top=10;$orderby=phenomenonTime desc;$select=phenomenonTime,result)"
)
OBS_PAGE_SIZE = 10  # must match $top above for gap detection

POLL_INTERVAL_DEFAULT = 300
METADATA_REFRESH_SECONDS = 86400
HTTP_TIMEOUT = 30
USER_AGENT = "stadtradhamburg-poller/0.1 (+https://github.com/PaulGoldschmidt/stadtradhamburg)"

OBS_SCHEMA = pa.schema(
    [
        ("datastream_id", pa.int32()),
        ("phenomenon_time", pa.timestamp("ns", tz="UTC")),
        ("result", pa.int32()),
        ("ingested_at", pa.timestamp("ns", tz="UTC")),
    ]
)

DS_SCHEMA = pa.schema(
    [
        ("id", pa.int32()),
        ("name", pa.string()),
        ("description", pa.string()),
        ("lon", pa.float64()),
        ("lat", pa.float64()),
        ("phenomenon_time_range", pa.string()),
        ("properties_json", pa.string()),
        ("refreshed_at", pa.timestamp("ns", tz="UTC")),
    ]
)


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


def get_json(session: requests.Session, url: str) -> dict[str, Any]:
    r = session.get(url, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def paginate(session: requests.Session, url: str) -> Iterator[dict[str, Any]]:
    while url:
        page = get_json(session, url)
        yield page
        url = page.get("@iot.nextLink") or ""


def initial_datastreams_url() -> str:
    return (
        f"{BASE_URL}"
        f"?$filter={quote(FILTER, safe='()/=')}"
        f"&$expand={quote(EXPAND_OBS, safe='()/=,;$')}"
        f"&$count=true"
    )


# ---------- State ---------------------------------------------------------------

def load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_state(path: Path, state: dict[str, str]) -> None:
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
        day = r["phenomenon_time"].astimezone(timezone.utc).date().isoformat()
        by_date.setdefault(day, []).append(r)

    total = 0
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    for day, day_rows in by_date.items():
        part_dir = data_dir / "observations" / f"date={day}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / f"part-{stamp}.parquet"
        tmp = out.with_suffix(".parquet.tmp")
        table = pa.Table.from_pylist(
            [
                {
                    "datastream_id": r["datastream_id"],
                    "phenomenon_time": r["phenomenon_time"],
                    "result": r["result"],
                    "ingested_at": now,
                }
                for r in day_rows
            ],
            schema=OBS_SCHEMA,
        )
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, out)
        total += len(day_rows)
        LOG.info("wrote %d obs → %s", len(day_rows), out)
    return total


def write_datastreams(items: list[dict[str, Any]], data_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    records = []
    for it in items:
        area = it.get("observedArea") or {}
        coords = area.get("coordinates") or []
        lon = lat = None
        # Two shapes observed in the wild:
        #   Point:      coordinates = [lon, lat]
        #   LineString: coordinates = [[EPSG:25832 x, y], [lon, lat]]
        # Pick the first [x, y] pair whose values are valid WGS84.
        candidates: list[list] = []
        if area.get("type") == "Point" and len(coords) == 2 and all(
            isinstance(c, (int, float)) for c in coords
        ):
            candidates.append(list(coords))
        else:
            candidates.extend(p for p in coords if isinstance(p, list) and len(p) == 2)
        for pair in candidates:
            if abs(pair[0]) <= 180 and abs(pair[1]) <= 90:
                lon, lat = float(pair[0]), float(pair[1])
                break
        records.append(
            {
                "id": int(it["@iot.id"]),
                "name": it.get("name"),
                "description": it.get("description"),
                "lon": lon,
                "lat": lat,
                "phenomenon_time_range": it.get("phenomenonTime"),
                "properties_json": json.dumps(
                    it.get("properties") or {}, ensure_ascii=False, sort_keys=True
                ),
                "refreshed_at": now,
            }
        )
    table = pa.Table.from_pylist(records, schema=DS_SCHEMA)
    out = data_dir / "datastreams.parquet"
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, out)
    LOG.info("wrote %d datastreams → %s", len(records), out)


# ---------- Core polling --------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    # SensorThings returns e.g. "2026-05-28T10:15:30.123Z" or "...+00:00".
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


def fetch_gap_fill(
    session: requests.Session, next_link: str, cutoff: datetime
) -> list[dict[str, Any]]:
    """Follow Observations@iot.nextLink for a single datastream until we cross
    `cutoff` (exclusive) or run out of pages. Returns raw obs dicts."""
    extra: list[dict[str, Any]] = []
    url = next_link
    while url:
        page = get_json(session, url)
        page_obs = page.get("value") or []
        keep = []
        crossed = False
        for o in page_obs:
            if parse_iso(o["phenomenonTime"]) > cutoff:
                keep.append(o)
            else:
                crossed = True
                break
        extra.extend(keep)
        if crossed:
            break
        url = page.get("@iot.nextLink") or ""
    return extra


def poll_once(
    session: requests.Session, state: dict[str, str], data_dir: Path
) -> tuple[int, int]:
    """Returns (datastreams_seen, observation_rows_written)."""
    new_rows: list[dict[str, Any]] = []
    seen = 0
    first_run = not state
    now_iso = datetime.now(timezone.utc).isoformat()

    for page in paginate(session, initial_datastreams_url()):
        for it in page.get("value", []):
            seen += 1
            ds_id = int(it["@iot.id"])
            ds_key = str(ds_id)
            obs = it.get("Observations") or []
            if not obs:
                # No observations at all — record cursor as now so we don't
                # repeatedly try to backfill an empty stream.
                state.setdefault(ds_key, now_iso)
                continue

            cutoff_str = state.get(ds_key)
            cutoff = parse_iso(cutoff_str) if cutoff_str else None

            # API returns obs newest-first; parse + filter to anything strictly
            # newer than our cursor.
            kept: list[tuple[datetime, int | None]] = []
            for o in obs:
                t = parse_iso(o["phenomenonTime"])
                if cutoff is None or t > cutoff:
                    kept.append((t, o.get("result")))

            # Gap detection: a full page of new obs may mean we missed more.
            if (
                cutoff is not None
                and len(kept) == OBS_PAGE_SIZE
                and it.get("Observations@iot.nextLink")
            ):
                LOG.warning(
                    "datastream %d: full page after cutoff %s — gap-filling",
                    ds_id,
                    cutoff_str,
                )
                extra = fetch_gap_fill(
                    session, it["Observations@iot.nextLink"], cutoff
                )
                for o in extra:
                    kept.append((parse_iso(o["phenomenonTime"]), o.get("result")))

            if not kept:
                continue

            newest = max(t for t, _ in kept)

            if first_run:
                # "Newest going forward": seed cursor, don't write history.
                state[ds_key] = newest.isoformat()
                continue

            for t, result in kept:
                new_rows.append(
                    {
                        "datastream_id": ds_id,
                        "phenomenon_time": t,
                        "result": int(result) if result is not None else None,
                    }
                )
            state[ds_key] = newest.isoformat()

    if first_run:
        # On first run, also seed any datastreams we saw with no obs above.
        LOG.info("first run: seeded cursors for %d datastreams (no obs written)", seen)
        written = 0
    else:
        written = write_observations(new_rows, data_dir)

    return seen, written


def refresh_metadata(session: requests.Session, data_dir: Path) -> None:
    items: list[dict[str, Any]] = []
    # Same filter, no $expand needed for metadata — cheaper.
    url = (
        f"{BASE_URL}?$filter={quote(FILTER, safe='()/=')}&$count=true"
    )
    for page in paginate(session, url):
        items.extend(page.get("value", []))
    write_datastreams(items, data_dir)


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
            seen, written = poll_once(session, state, data_dir)
            save_state(state_path, state)
            LOG.info("cycle done: %d datastreams seen, %d rows written", seen, written)
        except Exception:
            LOG.exception("poll cycle failed")

        if once or _stop:
            return 0

        elapsed = time.monotonic() - cycle_start
        sleep_for = max(1.0, interval - elapsed)
        # Sleep in short slices so SIGINT shuts us down promptly.
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
        default=Path(__file__).resolve().parent.parent / "data",
        help="Output directory (default: repo /data).",
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

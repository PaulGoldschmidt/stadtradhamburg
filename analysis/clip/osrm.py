"""Thin client for a self-hosted OSRM routing instance.

Default endpoint matches `deploy/osrm/docker-compose.yml` (`localhost:5000`).
Uses polyline6 geometry — six-decimal precision is overkill for visualization
but cheap to decode.
"""

from __future__ import annotations

from typing import Sequence

import polyline
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OSRM_DEFAULT = "http://localhost:5050"
USER_AGENT = "stadtradhamburg-clip/0.1"


class OSRMUnavailable(RuntimeError):
    pass


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def check_alive(base_url: str = OSRM_DEFAULT, timeout: float = 2.0) -> None:
    """Raise OSRMUnavailable with a setup-instruction message if OSRM isn't up."""
    try:
        r = requests.get(
            f"{base_url}/route/v1/cycling/9.99,53.55;10.01,53.55",
            params={"overview": "false"},
            timeout=timeout,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        raise OSRMUnavailable(
            f"OSRM not reachable at {base_url} ({e}). "
            "Bring it up with:\n"
            "  bash deploy/osrm/setup.sh\n"
            "  docker compose -f deploy/osrm/docker-compose.yml up -d"
        ) from None


def route(
    session: requests.Session,
    origin: tuple[float, float],
    destination: tuple[float, float],
    base_url: str = OSRM_DEFAULT,
) -> tuple[list[list[float]], float, float] | None:
    """Return (geometry as list of [lon, lat], distance_m, duration_s) or None.

    `origin` / `destination` are (lon, lat) pairs."""
    lon1, lat1 = origin
    lon2, lat2 = destination
    url = (
        f"{base_url}/route/v1/cycling/"
        f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
    )
    r = session.get(
        url,
        params={
            "overview": "full",
            "geometries": "polyline6",
            "alternatives": "false",
            "steps": "false",
        },
        timeout=10,
    )
    if r.status_code == 400:
        # Common for unreachable pairs (offshore stations, islands). Treat as
        # "no route" rather than raising.
        return None
    r.raise_for_status()
    body = r.json()
    if body.get("code") != "Ok" or not body.get("routes"):
        return None
    rt = body["routes"][0]
    # polyline.decode returns (lat, lon); flip to (lon, lat) for downstream.
    coords = [[lon, lat] for lat, lon in polyline.decode(rt["geometry"], 6)]
    return coords, float(rt["distance"]), float(rt["duration"])


def route_many(
    pairs: Sequence[tuple[str, str, tuple[float, float], tuple[float, float]]],
    base_url: str = OSRM_DEFAULT,
    max_workers: int = 8,
):
    """Concurrent fetch over (origin_id, dest_id, origin_lonlat, dest_lonlat).

    Yields tuples (origin_id, dest_id, geometry|None, distance_m|None,
    duration_s|None). OSRM-local: 8 parallel HTTP calls is generous and still
    polite (the routing engine itself is single-threaded per request but
    handles concurrent ones)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    session = make_session()
    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for oid, did, o, d in pairs:
            futures[ex.submit(route, session, o, d, base_url)] = (oid, did)
        for fut in as_completed(futures):
            oid, did = futures[fut]
            try:
                result = fut.result()
            except Exception:  # noqa: BLE001
                result = None
            if result is None:
                yield oid, did, None, None, None
            else:
                geom, dist, dur = result
                yield oid, did, geom, dist, dur

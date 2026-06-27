"""Build and load the per-(origin, destination) route cache."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

from clip.osrm import OSRM_DEFAULT, check_alive, route_many

ROUTE_SCHEMA = pa.schema(
    [
        ("origin_id", pa.string()),
        ("destination_id", pa.string()),
        # Geometry stored as JSON-encoded [[lon, lat], ...] — Parquet's nested
        # list types are fine too but JSON keeps the cache trivially portable.
        ("geometry_json", pa.string()),
        ("distance_m", pa.float64()),
        ("duration_s", pa.float64()),
        ("fetched_at", pa.timestamp("ns", tz="UTC")),
    ]
)


def cache_path(out_dir: Path) -> Path:
    return out_dir / "routes.parquet"


def load_cache(out_dir: Path) -> dict[tuple[str, str], dict]:
    p = cache_path(out_dir)
    if not p.exists():
        return {}
    t = pq.read_table(p)
    cache: dict[tuple[str, str], dict] = {}
    for row in t.to_pylist():
        cache[(row["origin_id"], row["destination_id"])] = {
            "geometry": json.loads(row["geometry_json"]) if row["geometry_json"] else None,
            "distance_m": row["distance_m"],
            "duration_s": row["duration_s"],
        }
    return cache


def needed_pairs(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, tuple[float, float], tuple[float, float]]]:
    """Distinct (origin_id, destination_id) from trips_geo, with both endpoints'
    lon/lat. Excludes pairs where either station has no coordinates."""
    rows = con.execute(
        """
        SELECT DISTINCT
               origin_id,        destination_id,
               origin_lon,       origin_lat,
               dest_lon,         dest_lat
        FROM trips_geo
        WHERE origin_lat IS NOT NULL AND origin_lon IS NOT NULL
          AND dest_lat   IS NOT NULL AND dest_lon   IS NOT NULL
        """
    ).fetchall()
    return [
        (oid, did, (olon, olat), (dlon, dlat))
        for (oid, did, olon, olat, dlon, dlat) in rows
    ]


def append_to_cache(out_dir: Path, batch: list[dict]) -> None:
    p = cache_path(out_dir)
    now = datetime.now(timezone.utc)
    table = pa.Table.from_pylist(
        [
            {
                "origin_id": r["origin_id"],
                "destination_id": r["destination_id"],
                "geometry_json": json.dumps(r["geometry"]) if r["geometry"] else None,
                "distance_m": r["distance_m"],
                "duration_s": r["duration_s"],
                "fetched_at": now,
            }
            for r in batch
        ],
        schema=ROUTE_SCHEMA,
    )
    if p.exists():
        existing = pq.read_table(p)
        table = pa.concat_tables([existing, table])
    tmp = p.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, p)


def ensure_routes(
    con: duckdb.DuckDBPyConnection,
    out_dir: Path,
    base_url: str = OSRM_DEFAULT,
    flush_every: int = 200,
) -> dict[tuple[str, str], dict]:
    """Idempotent: fetches only pairs missing from the cache. Returns the full
    cache as a dict keyed by (origin_id, destination_id)."""
    cache = load_cache(out_dir)
    all_pairs = needed_pairs(con)
    todo = [p for p in all_pairs if (p[0], p[1]) not in cache]
    if not todo:
        print(f"routes: cache covers all {len(all_pairs):,} pairs (no fetch needed)")
        return cache

    check_alive(base_url)
    print(
        f"routes: {len(cache):,} cached, fetching {len(todo):,} more "
        f"(of {len(all_pairs):,} total pairs)"
    )

    pending: list[dict] = []
    for oid, did, geom, dist, dur in tqdm(
        route_many(todo, base_url=base_url),
        total=len(todo),
        unit="route",
    ):
        pending.append(
            {
                "origin_id": oid,
                "destination_id": did,
                "geometry": geom,
                "distance_m": dist,
                "duration_s": dur,
            }
        )
        cache[(oid, did)] = {
            "geometry": geom,
            "distance_m": dist,
            "duration_s": dur,
        }
        if len(pending) >= flush_every:
            append_to_cache(out_dir, pending)
            pending = []

    if pending:
        append_to_cache(out_dir, pending)
    print(f"routes: cache now has {len(cache):,} pairs")
    return cache


def straight_line(origin_lonlat, destination_lonlat) -> list[list[float]]:
    """Fallback when a pair has no OSRM route. Two-point polyline."""
    return [list(origin_lonlat), list(destination_lonlat)]

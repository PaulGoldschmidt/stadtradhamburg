"""Incremental derivation of trips, bike state, latest station status and hourly
station sums from the Parquet store into `derived/` (layout: analysis/derived.py).

One-shot: processes what the pollers wrote since the previous run, writes the
results atomically, and exits — so its memory goes back to the host between
runs. `deploy/derive-loop.sh` runs it on a cadence in the `deriver` container.

Memory stays bounded no matter how long the store grows:

  - work is sliced by `date=` partition (one UTC day at a time), and within a
    partition to rows with `ingested_at` after the stored watermark — the hive
    `date` column prunes files, Parquet row-group statistics skip the rest;
  - the first run (no `derived/meta.json`) is the same loop over the whole
    history, so a bootstrap never sorts more than one day of bike sightings;
  - DuckDB gets an explicit memory limit (default 128 MB) and spills to
    `derived/tmp/` beyond that.

Trips are reconstructed with the shared definition in `analysis/_trips.py`:
the previous docked sighting of a bike is the `LAG` within the slice, falling
back to the carried `bike_state` for the first sighting of each slice. Every
output update is idempotent (set unions, min/max merges, per-day recomputes,
trip de-duplication on (bike, arrival time)), so a run that dies between
writing files and advancing the watermark is simply redone.

Rows ingested within the last GRACE seconds are left for the next run: a poll
cycle can span several partition files, and we must never advance the
watermark past a half-written cycle.

Usage:
    .venv/bin/python scripts/derive.py                 # one incremental pass (or bootstrap)
    .venv/bin/python scripts/derive.py --log-level DEBUG
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
from _trips import (  # noqa: E402
    LOCAL_TZ, build_views, configure_duckdb, data_root, partition_dates, trips_select,
)
import derived as D  # noqa: E402

LOG = logging.getLogger("derive")

GRACE_SEC = 90


# ---------- helpers -------------------------------------------------------------

def _ts(dt: datetime) -> str:
    """TIMESTAMPTZ literal for SQL."""
    return f"TIMESTAMPTZ '{dt.astimezone(timezone.utc).isoformat()}'"


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _newest_file(data_dir: Path, stream: str, dates: list[str]) -> str | None:
    """Name of the newest part file in the last two partitions — a cheap
    'anything new?' signal so idle runs do not open any Parquet file."""
    newest = None
    for d in dates[-2:]:
        for p in (data_dir / stream / f"date={d}").glob("part-*.parquet"):
            key = f"{d}/{p.name}"
            if newest is None or key > newest:
                newest = key
    return newest


def _candidate_dates(dates: list[str], watermark: datetime | None, back_days: int) -> list[str]:
    """Partitions that can hold rows newer than the watermark. Rows are
    partitioned by their own event time, which may lag `ingested_at` a little,
    hence the extra days back."""
    if watermark is None:
        return dates
    lo = (watermark.astimezone(timezone.utc).date() - timedelta(days=back_days)).isoformat()
    return [d for d in dates if d >= lo]


def _slice_where(day: str, watermark: datetime | None, cutoff: datetime) -> str:
    where = f"date = DATE '{day}' AND ingested_at <= {_ts(cutoff)}"
    if watermark is not None:
        where += f" AND ingested_at > {_ts(watermark)}"
    return where


def _max_ingested(con: duckdb.DuckDBPyConnection, table: str) -> datetime | None:
    v = con.execute(f"SELECT max(ingested_at) FROM {table}").fetchone()[0]
    return v.astimezone(timezone.utc) if v is not None else None


# ---------- bikes: trips + bike_state ----------------------------------------------

def _load_bike_state(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    p = D.bike_state_path(root)
    if p.exists():
        con.execute(f"CREATE OR REPLACE TEMP TABLE bike_state AS SELECT * FROM '{p}'")
    else:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE bike_state (
                bike_number VARCHAR, first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ,
                last_docked TIMESTAMPTZ, last_station VARCHAR)
            """
        )


def _append_trips(con: duckdb.DuckDBPyConnection, root: Path) -> int:
    """Merge `new_trips` into the per-arrival-day files, de-duplicated on
    (bike_number, arrived_by). Only the days present in the slice are touched."""
    days = [r[0].isoformat() for r in con.execute(
        "SELECT DISTINCT CAST(arrived_by AT TIME ZONE 'UTC' AS DATE) FROM new_trips ORDER BY 1"
    ).fetchall()]
    total = 0
    for day in days:
        path = D.trips_day_path(root, day)
        fresh = (
            "SELECT bike_number, origin_id, destination_id, departed_after, arrived_by "
            f"FROM new_trips WHERE CAST(arrived_by AT TIME ZONE 'UTC' AS DATE) = DATE '{day}'"
        )
        if path.exists():
            src = (f"SELECT bike_number, origin_id, destination_id, departed_after, arrived_by "
                   f"FROM '{path}' UNION ALL {fresh}")
        else:
            src = fresh
        final = (
            f"SELECT * FROM ({src}) "
            "QUALIFY row_number() OVER (PARTITION BY bike_number, arrived_by "
            "ORDER BY departed_after DESC) = 1 "
            "ORDER BY arrived_by, bike_number"
        )
        D.write_parquet(con, final, path)
        total += con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
    return total


def process_bikes(con: duckdb.DuckDBPyConnection, root: Path, data_dir: Path,
                  meta: dict, cutoff: datetime) -> int:
    dates = partition_dates(data_dir, "bikes")
    newest = _newest_file(data_dir, "bikes", dates)
    if newest is not None and newest == meta.get("bikes_newest_file") and meta.get("bikes_watermark"):
        LOG.debug("bikes: no new files")
        return 0
    watermark = _parse(meta.get("bikes_watermark"))
    _load_bike_state(con, root)
    processed = 0
    for day in _candidate_dates(dates, watermark, back_days=1):
        where = _slice_where(day, watermark, cutoff)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE slice AS "
            f"SELECT bike_number, station_id, observed_at, ingested_at FROM bikes WHERE {where}"
        )
        n = con.execute("SELECT count(*) FROM slice").fetchone()[0]
        if n == 0:
            continue
        t0 = time.monotonic()
        # Trips: LAG within the slice, carried state for each bike's first row.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE new_trips AS
            WITH ordered AS (
              SELECT s.bike_number, s.station_id, s.observed_at,
                     COALESCE(LAG(s.station_id)  OVER w, st.last_station) AS prev_station,
                     COALESCE(LAG(s.observed_at) OVER w, st.last_docked)  AS prev_seen
              FROM (SELECT * FROM slice WHERE station_id IS NOT NULL) s
              LEFT JOIN bike_state st ON st.bike_number = s.bike_number
              WINDOW w AS (PARTITION BY s.bike_number ORDER BY s.observed_at)
            )
            {trips_select("ordered")}
            """
        )
        n_trips = con.execute("SELECT count(*) FROM new_trips").fetchone()[0]
        _append_trips(con, root)
        # State: merge the slice's per-bike aggregates into the carried state.
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE agg AS
            SELECT bike_number,
                   min(observed_at) AS first_seen,
                   max(observed_at) AS last_seen,
                   max(observed_at) FILTER (WHERE station_id IS NOT NULL) AS last_docked,
                   arg_max(station_id, observed_at) FILTER (WHERE station_id IS NOT NULL) AS last_station
            FROM slice GROUP BY 1
            """
        )
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE bike_state_merged AS
            SELECT COALESCE(a.bike_number, s.bike_number) AS bike_number,
                   CASE WHEN s.first_seen IS NULL THEN a.first_seen
                        WHEN a.first_seen IS NULL THEN s.first_seen
                        ELSE least(a.first_seen, s.first_seen) END AS first_seen,
                   CASE WHEN s.last_seen IS NULL THEN a.last_seen
                        WHEN a.last_seen IS NULL THEN s.last_seen
                        ELSE greatest(a.last_seen, s.last_seen) END AS last_seen,
                   CASE WHEN a.last_docked IS NOT NULL
                         AND (s.last_docked IS NULL OR a.last_docked > s.last_docked)
                        THEN a.last_docked ELSE s.last_docked END AS last_docked,
                   CASE WHEN a.last_docked IS NOT NULL
                         AND (s.last_docked IS NULL OR a.last_docked > s.last_docked)
                        THEN a.last_station ELSE s.last_station END AS last_station
            FROM agg a FULL OUTER JOIN bike_state s ON s.bike_number = a.bike_number
            """
        )
        con.execute("DROP TABLE bike_state")
        con.execute("ALTER TABLE bike_state_merged RENAME TO bike_state")
        D.write_parquet(con, "SELECT * FROM bike_state ORDER BY bike_number", D.bike_state_path(root))
        watermark = _max_ingested(con, "slice")
        meta["bikes_watermark"] = watermark.isoformat()
        meta["bikes_newest_file"] = newest
        D.write_meta(root, meta)  # durable progress after every day
        processed += n
        LOG.info("bikes %s: %d sightings → %d trips (%.1fs)", day, n, n_trips, time.monotonic() - t0)
        con.execute("DROP TABLE slice")
        con.execute("DROP TABLE new_trips")
        con.execute("DROP TABLE agg")
    if processed == 0:
        meta["bikes_newest_file"] = newest
    return processed


# ---------- observations: obs_latest, station_hourly, obs_meta ------------------

def _load_obs_state(con: duckdb.DuckDBPyConnection, root: Path) -> None:
    p = D.obs_latest_path(root)
    if p.exists():
        con.execute(f"CREATE OR REPLACE TEMP TABLE obs_latest_t AS SELECT * FROM '{p}'")
    else:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE obs_latest_t (
                station_id VARCHAR, bikes BIGINT, docks BIGINT,
                installed BOOLEAN, as_of TIMESTAMPTZ)
            """
        )
    p = D.stations_seen_path(root)
    if p.exists():
        con.execute(f"CREATE OR REPLACE TEMP TABLE stations_seen AS SELECT * FROM '{p}'")
    else:
        con.execute("CREATE OR REPLACE TEMP TABLE stations_seen (station_id VARCHAR)")


def process_observations(con: duckdb.DuckDBPyConnection, root: Path, data_dir: Path,
                         meta: dict, cutoff: datetime) -> int:
    dates = partition_dates(data_dir, "observations")
    newest = _newest_file(data_dir, "observations", dates)
    if newest is not None and newest == meta.get("obs_newest_file") and meta.get("obs_watermark"):
        LOG.debug("observations: no new files")
        return 0
    watermark = _parse(meta.get("obs_watermark"))
    first_obs = _parse(meta.get("first_observation"))
    _load_obs_state(con, root)
    processed = 0
    for day in _candidate_dates(dates, watermark, back_days=2):
        where = _slice_where(day, watermark, cutoff)
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE slice AS
            SELECT station_id, last_reported, num_bikes_available, num_docks_available,
                   is_installed, ingested_at
            FROM observations WHERE {where}
            """
        )
        n = con.execute("SELECT count(*) FROM slice").fetchone()[0]
        if n == 0:
            continue
        t0 = time.monotonic()
        # Latest report per station: newest as_of wins.
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE fresh AS
            SELECT station_id,
                   arg_max(num_bikes_available, last_reported)::BIGINT AS bikes,
                   arg_max(num_docks_available, last_reported)::BIGINT AS docks,
                   arg_max(is_installed, last_reported)                AS installed,
                   max(last_reported)                                  AS as_of
            FROM slice GROUP BY 1
            """
        )
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE obs_latest_merged AS
            SELECT COALESCE(f.station_id, o.station_id) AS station_id,
                   CASE WHEN f.as_of IS NOT NULL AND (o.as_of IS NULL OR f.as_of >= o.as_of)
                        THEN f.bikes ELSE o.bikes END AS bikes,
                   CASE WHEN f.as_of IS NOT NULL AND (o.as_of IS NULL OR f.as_of >= o.as_of)
                        THEN f.docks ELSE o.docks END AS docks,
                   CASE WHEN f.as_of IS NOT NULL AND (o.as_of IS NULL OR f.as_of >= o.as_of)
                        THEN f.installed ELSE o.installed END AS installed,
                   CASE WHEN f.as_of IS NOT NULL AND (o.as_of IS NULL OR f.as_of >= o.as_of)
                        THEN f.as_of ELSE o.as_of END AS as_of
            FROM fresh f FULL OUTER JOIN obs_latest_t o ON o.station_id = f.station_id
            """
        )
        con.execute("DROP TABLE obs_latest_t")
        con.execute("ALTER TABLE obs_latest_merged RENAME TO obs_latest_t")
        # Per-day (station, local hour) sums, recomputed from the whole partition
        # so the update is idempotent. Mirrors the web's old station_hourly query.
        D.write_parquet(
            con,
            f"""
            SELECT station_id,
                   EXTRACT(hour FROM last_reported AT TIME ZONE '{LOCAL_TZ}')::INT AS hour,
                   sum(num_bikes_available)::BIGINT AS sum_bikes,
                   count(*)::BIGINT AS n
            FROM observations
            WHERE date = DATE '{day}' AND is_installed
            GROUP BY 1, 2 ORDER BY 1, 2
            """,
            D.station_hourly_day_path(root, day),
        )
        con.execute(
            "INSERT INTO stations_seen SELECT DISTINCT station_id FROM slice "
            "WHERE station_id NOT IN (SELECT station_id FROM stations_seen)"
        )
        lo = con.execute("SELECT min(last_reported) FROM slice").fetchone()[0]
        if lo is not None and (first_obs is None or lo < first_obs):
            first_obs = lo.astimezone(timezone.utc)
        D.write_parquet(con, "SELECT * FROM obs_latest_t ORDER BY station_id", D.obs_latest_path(root))
        D.write_parquet(con, "SELECT station_id FROM stations_seen ORDER BY 1", D.stations_seen_path(root))
        D.write_parquet(
            con,
            f"SELECT {_ts(first_obs)} AS first_observation, "
            "(SELECT count(*) FROM stations_seen)::BIGINT AS stations",
            D.obs_meta_path(root),
        )
        watermark = _max_ingested(con, "slice")
        meta["obs_watermark"] = watermark.isoformat()
        meta["obs_newest_file"] = newest
        meta["first_observation"] = first_obs.isoformat()
        D.write_meta(root, meta)
        processed += n
        LOG.info("observations %s: %d rows (%.1fs)", day, n, time.monotonic() - t0)
        con.execute("DROP TABLE slice")
        con.execute("DROP TABLE fresh")
    if processed == 0:
        meta["obs_newest_file"] = newest
    return processed


# ---------- main ----------------------------------------------------------------

def run(data_dir: Path, root: Path, grace_sec: int, memory_limit: str, threads: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    configure_duckdb(con, memory_limit=memory_limit, threads=threads, temp_dir=D.tmp_dir(root))
    con.execute("SET preserve_insertion_order = false")  # every write sorts explicitly; saves memory
    build_views(con, data_dir)

    meta = D.read_meta(root)
    if meta is not None and meta.get("schema_version") != D.SCHEMA_VERSION:
        LOG.warning("derived schema %s != %s: resetting %s",
                    meta.get("schema_version"), D.SCHEMA_VERSION, root)
        D.reset(root)
        meta = None
    if meta is None:
        LOG.info("no derived store at %s — bootstrapping from the full history", root)
        meta = {"schema_version": D.SCHEMA_VERSION}

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=grace_sec)
    t0 = time.monotonic()
    nb = process_bikes(con, root, data_dir, meta, cutoff)
    no = process_observations(con, root, data_dir, meta, cutoff)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    D.write_meta(root, meta)
    LOG.info("done: %d bike sightings, %d observations in %.1fs (watermarks bikes=%s obs=%s)",
             nb, no, time.monotonic() - t0, meta.get("bikes_watermark"), meta.get("obs_watermark"))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=None,
                   help="Parquet store (default: autodetect via _trips.data_root()).")
    p.add_argument("--derived-dir", type=Path, default=None,
                   help="Output dir (default: $DERIVED_DIR or <repo>/derived).")
    p.add_argument("--grace-sec", type=int, default=GRACE_SEC,
                   help=f"Leave rows ingested in the last N seconds for the next run (default {GRACE_SEC}).")
    p.add_argument("--memory-limit", default="128MB", help="DuckDB memory_limit (default 128MB).")
    p.add_argument("--threads", type=int, default=1, help="DuckDB threads (default 1).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    data_dir = args.data_dir or data_root()
    root = args.derived_dir or D.derived_root()
    return run(data_dir, root, args.grace_sec, args.memory_limit, args.threads)


if __name__ == "__main__":
    sys.exit(main())

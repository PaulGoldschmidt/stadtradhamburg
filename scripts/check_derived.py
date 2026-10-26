"""Equivalence check: the incrementally derived store (`derived/`) must equal a
full rebuild from the raw Parquet streams.

Compares, with exact set semantics:

  - trips           full `_trips.build_trips()` vs `derived/trips/`
  - bike_last       full per-bike aggregate vs `derived/bike_state.parquet`
  - obs_latest      full latest-report-per-station vs `derived/obs_latest.parquet`
  - station_hourly  full-scan (station, hour) average vs the per-day sums
  - obs_meta        first observation + distinct station count

The full rebuild is the memory-hungry path the production web no longer runs;
this script is meant for a laptop or CI, not the host. Run the deriver first
(`scripts/derive.py`) until it reports nothing new, then:

    .venv/bin/python scripts/check_derived.py

Exit code 0 = everything matches. Rows the deriver has intentionally not yet
consumed (ingested within its grace window) would show up as differences, so
run the check on a quiet store.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analysis"))
from _trips import LOCAL_TZ, build_trips, build_views, data_root  # noqa: E402
import derived as D  # noqa: E402


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0]


def _diff(con: duckdb.DuckDBPyConnection, name: str, full: str, derived: str,
          sample: int = 3) -> bool:
    only_full = _count(con, f"{full} EXCEPT {derived}")
    only_derived = _count(con, f"{derived} EXCEPT {full}")
    n_full, n_derived = _count(con, full), _count(con, derived)
    ok = only_full == 0 and only_derived == 0 and n_full == n_derived
    print(f"{'PASS' if ok else 'FAIL'} {name}: full={n_full} derived={n_derived} "
          f"only_in_full={only_full} only_in_derived={only_derived}")
    if not ok and sample:
        for label, sql in (("only in full", f"{full} EXCEPT {derived}"),
                           ("only in derived", f"{derived} EXCEPT {full}")):
            rows = con.execute(f"{sql} LIMIT {sample}").fetchall()
            for r in rows:
                print(f"    {label}: {r}")
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument("--derived-dir", type=Path, default=None)
    args = p.parse_args(argv)
    data_dir = args.data_dir or data_root()
    root = args.derived_dir or D.derived_root()

    con = duckdb.connect()
    build_views(con, data_dir)
    present = D.build_derived_views(con, root, LOCAL_TZ)
    missing = [k for k, v in present.items() if not v]
    if missing:
        print(f"FAIL derived store at {root} is incomplete: missing {missing}")
        return 1

    # Derived views are named like the old tables; keep them under d_* and
    # build the full versions under their usual names.
    for v in ("trips", "bike_last", "obs_latest", "station_hourly", "obs_meta"):
        con.execute(f"CREATE VIEW d_{v} AS SELECT * FROM {v}")
    con.execute("DROP VIEW trips_geo")
    con.execute("DROP VIEW trips")
    print("full rebuild of trips (this is the heavy path) …")
    build_trips(con)

    cols = "bike_number, origin_id, destination_id, departed_after, arrived_by"
    ok = True
    ok &= _diff(con, "trips",
                f"SELECT {cols} FROM trips",
                f"SELECT {cols} FROM d_trips")
    ok &= _diff(con, "bike_last",
                """SELECT bike_number, min(observed_at) AS first_seen, max(observed_at) AS last_seen,
                          max(observed_at) FILTER (WHERE station_id IS NOT NULL) AS last_docked,
                          arg_max(station_id, observed_at) FILTER (WHERE station_id IS NOT NULL) AS station_id
                   FROM bikes GROUP BY 1""",
                "SELECT bike_number, first_seen, last_seen, last_docked, station_id FROM d_bike_last")
    ok &= _diff(con, "obs_latest",
                """SELECT station_id,
                          arg_max(num_bikes_available, last_reported)::BIGINT AS bikes,
                          arg_max(num_docks_available, last_reported)::BIGINT AS docks,
                          arg_max(is_installed, last_reported)                AS installed,
                          max(last_reported)                                  AS as_of
                   FROM observations GROUP BY 1""",
                "SELECT station_id, bikes, docks, installed, as_of FROM d_obs_latest")
    ok &= _diff(con, "station_hourly",
                f"""SELECT station_id,
                           EXTRACT(hour FROM last_reported AT TIME ZONE '{LOCAL_TZ}')::INT AS hour,
                           round(avg(num_bikes_available), 2) AS avg_bikes
                    FROM observations WHERE is_installed GROUP BY 1, 2""",
                "SELECT station_id, hour, avg_bikes FROM d_station_hourly")
    ok &= _diff(con, "obs_meta",
                """SELECT (SELECT min(last_reported) FROM observations) AS first_observation,
                          (SELECT count(DISTINCT station_id) FROM observations)::BIGINT AS stations""",
                "SELECT first_observation, stations FROM d_obs_meta")
    print("ALL PASS" if ok else "MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

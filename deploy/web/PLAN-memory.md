# Plan: bounded-memory serving for the web and forecast services

Status: implemented in the working tree on 2026-10-26 (uncommitted, not yet
deployed); the host still runs the stop-gap described below. Measured results
of the implementation are in `deploy/web/README.md` ("Budget") and at the end
of this file. Written after a read-only diagnosis of the production host
(rutherford) and a read of this repo.

## What went wrong in production

Measured on rutherford on 2026-10-26 (`deploy/web/docker-compose.yml`, three
containers off one image, data store with ~150 daily partitions, `data/bikes`
5.3 GB and `data/observations` 0.5 GB of zstd Parquet):

| Service | Limit then | Observed |
|---|---|---|
| `stadtrad-web` | 3 g (512 m before 2026-09-11) | reached the limit within 3 min of every start, spilled 3+ GB of DuckDB temp files into the container overlay, thrashed through the swap allowance, was OOM-killed and restarted; 2238 restarts since 2026-09-11 (5 to 28 per hour) |
| `stadtrad-forecast` | 1 g | `--predict` OOM-killed on every daytime run since 2026-09-21 (11 kills per hour); only the night runs completed |
| host | | 5 to 20 MB/s swap in and out around the clock; 334 TB written to each NVMe in 75 days (150 TB swap, 184 TB DuckDB spill), on drives rated 300 TBW |

Compose's `mem_limit` without `memswap_limit` gives a container a swap
allowance equal to its memory limit, so instead of failing fast the two
containers paged through swap for minutes before each kill.

## Root causes in this repo

1. `web/db.py:refresh_trips()` calls `_trips.build_trips()`, which sorts the
   entire `bikes` history (`LAG ... OVER (PARTITION BY bike_number ORDER BY
   observed_at)`) at every start and every `TRIP_REFRESH_SEC` (300 s). With
   ~170 M rows that sort exceeds DuckDB's memory limit and spills to
   `/app/.tmp`, which sits on the container's overlay filesystem. This is the
   source of the root-filesystem writes and of the 100 % CPU.
2. `web/db.py:_refresh_history_tables()` rebuilds `bike_last` with a full scan
   of `bikes` every 5 minutes (cheap in memory, 5 GB of reads each time).
3. `analysis/forecast.py:build_panel()` with `lookback_hours` set still reads
   the full history three times: `cap` (`quantile_cont` over every
   observation, which materialises every row), `bounds` (min/max over the
   full glob) and the `ASOF LEFT JOIN` of the 48 h grid against the unbounded
   `obs_raw` (the right side is sorted in memory). Memory grows with history;
   1 g stopped being enough in September, 3 g was still killed once on
   2026-10-26, 6 g works today.
4. `predict_live()` ignores `bundle["capacity"]`, which `save_bundle()` stores
   exactly so that serving can reproduce the training capacity without a
   history scan.
5. `deploy/forecast-loop.sh` runs `analysis/weather.py` every cycle, whose
   `detect_window()` does a min/max over the full observations glob (I/O only,
   but 0.5 GB per cycle).
6. Neither service sets DuckDB's `memory_limit` or `temp_directory`. DuckDB
   (1.5.4 in the image) derives its default limit from the cgroup limit
   (80 % of it) and spills beyond that into `.tmp` under the working
   directory, i.e. the overlay.
7. `refresh_trips()` holds the single connection lock for the whole rebuild
   (about 2 min today), so every request including `/api/health` blocks
   meanwhile; the container flips to "unhealthy" during each 5-minute cycle
   and back afterwards. Design B removes this entirely: the web no longer
   rebuilds anything.

## Stop-gap (applied on the host 2026-10-26, not yet in git)

`deploy/web/docker-compose.yml` on rutherford now has `memswap_limit` equal
to `mem_limit` for all three services (no swap allowance: a container that
outgrows its limit is killed and restarted instead of thrashing), with web
10 g (6 g survived the startup build but was OOM-killed on the first
periodic trips rebuild with 6.26 GB resident, because the old tables and
the new build coexist), forecast 6 g (3 g was killed once) and poller
256 m. The host checkout
has this as an uncommitted local change on top of `965da26`; the backup is
`docker-compose.yml.bak-2026-10-26` next to it. This buys time, nothing
else: the working set still grows with every daily partition.

Measured right after the change (2026-10-26 18:48 to 18:55 CET): the web
container built its cache in 2.5 min with a cgroup peak of 6.1 GB (4.4 GB
anonymous, the rest page cache), no temp spill, no OOM kill, healthy since;
the forecast's first daytime predict run completed in about 30 s with a peak
of 4.4 GB; swap usage of all three containers is 0 and host swap I/O dropped
to zero. The web's periodic rebuild then needed more than 6 GB (killed at
6.26 GB resident), so it was raised to 10 g at 19:01 CET. Today's real
working sets are therefore roughly 6.5 GB (web, during a rebuild) and
4.4 GB (forecast), which is what sections A to C shrink to well under
1 GB in total. At 10 g the periodic rebuild completed (19:10 CET) with a cgroup peak
at the limit, a transient DuckDB spill of about 0.4 GB per cycle, and no
kill. The rebuild still reads 5.3 GB and sorts the full history every
5 minutes; raising `TRIP_REFRESH_SEC` (for example to 1800) would cut that
load six-fold until design A lands, at the cost of trip statistics that
are up to 30 minutes old. Not applied; Paul's call.

## Design principle (Paul, 2026-10-26)

Keep the containers as light as possible and use as little RAM on the host
as possible. Disk writes are fine. Where a choice trades memory against
speed, take the low-memory option even if requests get slower or data is a
few minutes less fresh. Every decision below follows that rule.

## Target design

Three rules fall out of the principle:

1. No process keeps derived state in memory. Everything derived is written
   to small Parquet files on disk and read back through DuckDB views, the
   same pattern the poller and the forecast already use (atomic `*.tmp` plus
   `os.replace`).
2. Heavy work runs in short-lived processes that exit when done, so their
   memory goes back to the host between runs. The forecast already works
   like this; derivation will too.
3. Every job touches only the partitions that changed since its last run.
   Full-history work happens once (bootstrap), replayed day by day, so
   memory is bounded by one day of data (about 1.2 M bike rows) regardless
   of how long the store grows.

The web process ends up as a thin, read-only query layer with a small
DuckDB buffer pool; the writer is a separate one-shot "deriver".

### A. Deriver: incremental derived Parquet (new, replaces the web's rebuilds)

A new one-shot script, `scripts/derive.py` (same image, run by
`deploy/derive-loop.sh` as service `deriver`, like the forecast loop), run
every `DERIVE_INTERVAL` seconds (60 is fine: it costs nothing when there is
no new data). It keeps a `derived/` directory next to `data/` (host bind
mount, read-write for the deriver, read-only for the web):

| File | Content | Updated |
|---|---|---|
| `derived/meta.json` | watermark (max `observed_at` processed), `first_observation`, schema version | every run |
| `derived/bike_state.parquet` | one row per bike: `first_seen`, `last_seen`, `last_docked`, `last_station` (today's `bike_last`) | every run (about 4.5 k rows) |
| `derived/trips/date=YYYY-MM-DD/trips.parquet` | trips by arrival day, same columns as today's `trips` | only the current day's file is rewritten (a few thousand rows); past days never change |
| `derived/obs_latest.parquet` | latest report per station ever seen (today's `obs_latest`) | every run (359 rows) |
| `derived/station_hourly.parquet` | per (station, local hour): `sum_bikes`, `n`, from which `avg_bikes` is a view | every run, incremental sums |
| `derived/stations_seen.parquet` | distinct station ids ever observed (for `obs_meta.stations`) | every run |

One run does: read `meta.json`; list the `bikes` and `observations`
partitions (directory listing, as `db.py:_obs_partition_dates()` does
today; move that helper to `analysis/_trips.py`); select new rows with
`date >= date(W) AND observed_at > W` (hive partition pruning); compute
trips for the slice with `LAG` inside the slice and `bike_state` as the
carried previous sighting; append them to the arrival-day files; upsert
`bike_state`; merge the slice into `obs_latest`, `station_hourly`,
`stations_seen`; write everything atomically; advance the watermark. With no
`meta.json` it bootstraps by looping over the history one day at a time.
The trip predicate (`station_id IS NOT NULL`, `prev_station <> station_id`,
gap `< TRIP_CAP`) is defined once in `analysis/_trips.py` and used by both
the deriver and the existing `build_trips()` (which stays for `stats.py`,
the clip pipeline and the equivalence test).

DuckDB settings for a run: `memory_limit` 128 MB, `threads` 1,
`temp_directory` under `derived/tmp`. A run on new data is a few thousand
rows; a bootstrap day is about 1.2 M rows, which DuckDB sorts within that
limit or spills to `derived/tmp`. Container `mem_limit: 256m` with
`memswap_limit: 256m`; idle footprint is one `bash` and a `sleep`.

Why Parquet files and not a DuckDB database file: a DuckDB file cannot be
open for writing in one process while another process reads it, so a
file-based cache would force the writer back into the web process, which
is exactly the memory-holding, lock-holding rebuild we are removing. Parquet
files also keep the web stateless and match the repo's existing pattern.

### B. Web: stateless reader

- `web/db.py` shrinks to: an in-memory DuckDB connection with views over
  `data/` and `derived/` globs (`bikes`, `observations`, `stations`,
  `weather`, `forecast_live`, `trips`, `bike_last`, `obs_latest`,
  `station_hourly` as `sum/n`, `obs_meta` from `meta.json` plus
  `stations_seen`), plus the lock and `query_df()`. `trips_geo` becomes a
  view over `trips` joined to `stations`. No `refresh_trips()`, no
  `_refresh_history_tables()`, no background rebuild tasks.
- `web/app.py` drops `_trip_refresher`; `_live_refresher` is reduced to
  re-reading `meta.json` (for `max_obs_date()`) or removed; cache
  pre-warming (`warm_history`, `warm_live`) is removed, the first request
  after a TTL expiry pays the query instead. Views over globs pick up new
  derived files at query time, so nothing needs to be told about updates.
- DuckDB settings: `memory_limit` from `DUCKDB_MEMORY_LIMIT` (default
  256 MB), `threads` 1, `temp_directory` on a small named volume or `/tmp`,
  `max_temp_directory_size` 2 GB. History endpoints scan the `trips` Parquet
  files from disk on each cache refill (about 1 M rows today, growing by a
  few thousand per day); tens to a few hundred milliseconds on this host,
  which the principle accepts. `web/queries.py` keeps its TTL caches: they
  hold small JSON-sized results, and they bound the scan rate.
- Container `mem_limit: 384m` and `memswap_limit: 384m` (Python plus
  pandas, pyarrow and DuckDB baseline is roughly 150 to 200 MB; verify and
  tighten). Health check stays; it no longer blocks during rebuilds because
  there are none.
- Optional later step to shave another 50 to 100 MB: serve API results
  straight from DuckDB (`fetchall` or Arrow) instead of pandas DataFrames in
  the web process, so pandas is not imported at all there.

### C. Forecast: bounded, one bundle at a time

- `build_panel(..., lookback_hours, capacity=None)`: when `lookback_hours`
  is set, take `t1` from the newest partition only, compute
  `lo = date(t1 - lookback_hours - outage_cap_hours - 1 day)` and define
  `obs_raw` as the glob filtered with `WHERE date >= DATE 'lo'`. Correctness:
  the panel nulls every slot whose newest observation is older than
  `outage_cap_hours`, so earlier observations can never contribute a kept
  value; the output is identical to today's. Lower the serving lookback from
  48 h to 12 h (the features need 60 min of lags plus the 2 h outage cap);
  the panel is then about 17 k rows.
- `cap` from `bundle["capacity"]` (stored by `save_bundle()` precisely for
  serving, unused today); stations missing from the bundle get the windowed
  quantile as a logged fallback. This also removes a train/serve skew.
- Load one bundle at a time inside the horizon loop and release it before
  the next (today all four are loaded up front); check file existence first
  to keep the fail-early behaviour.
- `weather.detect_window()` uses the first and last partition instead of
  the full glob.
- `forecast.py` and `weather.py` honour `DUCKDB_THREADS` (passed by compose
  today but ignored) and a new `DUCKDB_MEMORY_LIMIT` (default 128 MB).
- Container `mem_limit: 512m` and `memswap_limit: 512m`: the floor is the
  Python plus pandas, LightGBM and scikit-learn imports (roughly 250 to
  300 MB); a run then needs a few tens of MB. `FORECAST_INTERVAL` can stay
  at 300 s because a run takes seconds. If a run ever exceeds the limit it
  is killed and simply retried next tick, as today, but without swap.

### D. Compose and image

- Services: `web` (reader), `poller` (unchanged, `256m`/`256m`), `deriver`
  (new, `256m`/`256m`, mounts `data/` read-only and `derived/` read-write),
  `forecast` (`512m`/`512m`). Expected host RAM for the whole stack: well
  under 1 GB resident, about 1.4 GB in limits, against 16.25 GB in limits
  today.
- Standing rule for the compose file: `memswap_limit` always equals
  `mem_limit`, `cpus` caps stay, `DUCKDB_THREADS=1` for web, deriver and
  forecast.
- Host prerequisite: `derived/` writable by uid 1000, like `data/` and
  `analysis/out/` already are.
- No new dependencies. The Dockerfile additionally copies
  `scripts/derive.py` and `deploy/derive-loop.sh`.
- Docs: `deploy/web/README.md` (service table, the `derived/` directory,
  how to force a rebuild by deleting `derived/meta.json`, the removed
  `mem_limit: 512m` note and the "data grows over months" caveat),
  `deploy/forecast.md` (scaling note resolved), `CLAUDE.md` architecture
  section (web layer now stateless, derivation in `scripts/derive.py`).

### E. Verification

Local (this checkout has 36 daily partitions under `data/`, the four model
bundles in `analysis/out/`, and a `.venv` with duckdb 1.5.4, pandas 3.0.3,
lightgbm 4.6.0):

1. Trips equivalence: `build_trips()` over the full local store versus the
   bootstrapped `derived/trips/` must give the same row count and the same
   set of `(bike_number, origin_id, destination_id, departed_after,
   arrived_by)`. Then hide the last partition, bootstrap, unhide it, run one
   deriver pass and compare against a fresh full rebuild again. Same for
   `bike_state` against today's `bike_last` query and `station_hourly`
   against today's full-scan query.
2. Forecast equivalence: run `--predict --horizons 30 60 90 120 --no-map`
   with the current code and with the bounded code on the same data
   snapshot and compare the two `forecast_live.parquet` files (exact for
   `bikes_now` and `capacity`, float tolerance for `pred_bikes` and
   `p_empty`).
3. Peak memory of each process (`resource.getrusage` or `/usr/bin/time -l`):
   targets under 200 MB for a deriver run and a bootstrap day, under 300 MB
   for the web process after serving every endpoint once, under 400 MB for
   a predict run.
4. Latency budget, measured and written into the README so nobody later
   "optimises" it back into memory: `/api/history/*` cold (after TTL expiry)
   and warm, `/api/station/<id>`, `/api/forecast?horizon=60`.
5. Web smoke test with `uvicorn web.app:app`: `/api/health`,
   `/api/history/summary` (trips count equal to the full rebuild),
   `/api/station/<id>`, `/api/forecast?horizon=60`.

Production (rutherford):

6. Before deploying, record `/api/history/summary` (trips, unique_bikes,
   first_seen). The same numbers must come back after the bootstrap. For
   reference, on 2026-10-26 18:54 CET it returned trips 940488, unique_bikes
   4454, stations 359, first_seen 2026-05-29T16:46:51Z.
7. Deploy as documented in `deploy/web/README.md`, with one extra step first:
   the host checkout carries the uncommitted stop-gap in the compose file, so
   run `git checkout -- deploy/web/docker-compose.yml` there before
   `git pull`, then `mkdir -p derived && chown 1000:1000 derived`, then
   `docker compose -f deploy/web/docker-compose.yml up --build -d`. The
   deriver's bootstrap over 150 days runs day by day and takes some minutes;
   the web serves empty history until `derived/trips/` exists (document
   that).
8. Watch for 24 h: `memory.peak` of every container, `memory.events`
   (`oom_kill` stays 0), `memory.swap.current` (0), no `.tmp` growth in the
   web overlay, "prediction completed" every cycle, host `sar -W` near zero,
   the md2 write rate in `/proc/diskstats` (a few MB per deriver run is the
   expected write load).

### F. Suggested commit order (nothing committed yet)

1. Compose: `memswap_limit` plus the limits currently running on the host, so
   the host's local diff disappears on the next pull.
2. Forecast: bounded panel, bundle capacity, one bundle at a time, DuckDB
   limits, cheap weather window. Small and independent.
3. Deriver plus derived Parquet layout, with the equivalence tests from E.1
   runnable as a script (`scripts/check_derived.py`).
4. Web: stateless reader, removal of the rebuild tasks and pre-warming,
   DuckDB limits, new compose limits and the `deriver` service.
5. Docs: `deploy/web/README.md`, `deploy/forecast.md`, `CLAUDE.md`.

Rollback for each step is a revert plus `up --build -d`; `derived/` can be
deleted at any time and is rebuilt by the next deriver run.

### G. Decisions for Paul

- Deriver cadence: 60 s polling with a cheap "anything new?" check
  (recommended) or simply the poller's 300 s.
- Whether the forecast should drop pandas in favour of plain DuckDB and
  NumPy to get the container under 256 MB (worth it only if the host stays
  tight after the rest lands).
- Retraining: the bundles date from July; stations added since then only get
  the windowed capacity fallback. A periodic off-box retrain (as described in
  `deploy/web/README.md`) refreshes prior and capacity.

## Implementation notes (2026-10-26)

What landed, against the sections above:

- A: `scripts/derive.py`, `analysis/derived.py`, `deploy/derive-loop.sh`,
  `scripts/check_derived.py`. Trips are de-duplicated on (bike, arrival time),
  the watermark is `ingested_at` with a 90 s grace window (a poll cycle can
  span several partition files), bootstrap and incremental passes share one
  code path. Equivalence on the local 36-day store: trips, bike_last,
  obs_latest, station_hourly and obs_meta all identical to a full rebuild,
  also after hiding and restoring the newest poll files in two steps.
- B: `web/db.py` rewritten as a reader, `web/app.py` keeps one view
  refresher, `web/queries.py` lost its warming, `web/settings.py` gained
  `DERIVED_DIR`, `DUCKDB_MEMORY_LIMIT`, `DUCKDB_TEMP_DIR`. `scripts/smoke_web.py`
  is the budget check.
- C: `analysis/forecast.py` bounded panel, bundle capacity with windowed
  fallback, one bundle at a time, DuckDB bounds; `analysis/weather.py` reads
  only the first and last partition. Output at `--lookback-hours 48` is
  bit-identical to the old code on the same data; the container uses 24 h,
  which drops stations silent for a day. Peak RSS 470 MB versus 1.7 GB on the
  laptop store (4.4 GB on the host).
- D: compose with the `deriver` service, limits web 384m, poller 256m,
  deriver 512m (bootstrap peak), forecast 640m; Dockerfile copies the new
  files; `/derived` gitignored. Docs updated (`deploy/web/README.md`,
  `deploy/forecast.md`, `CLAUDE.md`).

Deployment checklist (E.6/E.7) is unchanged: record `/api/history/summary`,
`git checkout -- deploy/web/docker-compose.yml` on the host, `git pull`,
`mkdir -p derived && chown 1000:1000 derived`, `up --build -d`, then watch the
deriver bootstrap (about 150 days, a few seconds each) before the history
pages fill.

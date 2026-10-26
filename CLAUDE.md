# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A data-science project around Hamburg's StadtRad bike-share. Three pieces: **pollers** that snapshot a live API into a partitioned Parquet store, an **analysis** layer that reconstructs trips from those snapshots (stats reports, a stockout forecast, animated MP4 clips), and a **web** layer (`web/`) — the FastAPI dashboard + read-only JSON API deployed at rad.haw-hamburg.eu.

There are no tests, no build step, and no linter configured. "Running" means running a poller, an analysis script, or the web app.

## Environment

Python 3.14 in a local `.venv` (the web container in `deploy/web/Dockerfile` pins 3.12). Always invoke scripts through it:

```sh
source .venv/bin/activate          # or prefix commands with .venv/bin/python
pip install -r requirements.txt
```

Credentials for the GBFS poller live in `.env` at the repo root, with **lowercase** keys `clientid` and `clientsecret` (Deutsche Bahn API marketplace). The poller maps these to `DB-Client-ID` / `DB-Api-Key` headers.

## Common commands

```sh
# Active poller (Deutsche Bahn GBFS) — see "Two pollers" below
.venv/bin/python scripts/gbfs_poller.py --once        # one cycle (smoke test / cron)
.venv/bin/python scripts/gbfs_poller.py               # daemon, 5-min cadence

# Stats report → analysis/out/stats_report.md + PNGs
.venv/bin/python analysis/stats.py

# Derived store for the web (trips, state); rerun to fold in new poller output
.venv/bin/python scripts/derive.py
.venv/bin/python scripts/check_derived.py             # derived/ == full rebuild?

# Web dashboard + API (needs requirements-web.txt installed)
.venv/bin/python -m uvicorn web.app:app --reload --port 8000
.venv/bin/python scripts/smoke_web.py                 # every endpoint, cold+warm, RSS

# Notebook pipeline (trip inference + interactive maps)
jupyter notebook analysis/trips.ipynb

# Trips clip (requires OSRM running + ffmpeg on PATH)
.venv/bin/python analysis/clip.py --day 2026-06-23 --routes-only   # warm route cache first
.venv/bin/python analysis/clip.py --day 2026-06-23                 # render
.venv/bin/python scripts/preview_frame.py --day 2026-06-23 --at-hour 17.5   # single-frame look check

# OSRM (one-time graph build, then run)
bash deploy/osrm/setup.sh
docker compose -f deploy/osrm/docker-compose.yml up -d              # listens on :5050, NOT :5000
```

## Architecture

### Two pollers, one shared shape

- `scripts/stadtrad_poller.py` — Hamburg SensorThings API (`iot.hamburg.de`). **Dormant**: the upstream feed has been silent since 2026-03-31. Kept for reference.
- `scripts/gbfs_poller.py` — the **active** source. Polls Deutsche Bahn's Shared-Mobility GBFS API. This is what produces current data.

Both pollers are independent files (no shared module) but follow the identical pattern, so a change to one usually belongs in the other:

- **Cursor-based dedup via `state.json`**: per-stream last-seen timestamp; only strictly-newer observations get written.
- **First run seeds cursors only** — it intentionally writes zero observations, so the dataset begins at the moment polling started. Don't "fix" this.
- **Atomic writes everywhere**: write to `*.tmp`, then `os.replace`. State and every Parquet part use this.
- **Partitioned Parquet output**: `observations/date=YYYY-MM-DD/part-<UTC-stamp>.parquet`, zstd-compressed, one file per cycle. Metadata (`stations.parquet` / `datastreams.parquet`) refreshes daily. Every row carries `ingested_at` (the write time, identical for a whole cycle); the deriver uses it as its watermark, so keep it.
- Daemon loop catches all exceptions per-cycle (never dies on a bad poll), sleeps in 1-second slices for prompt SIGTERM shutdown.

GBFS-specific: `free_bike_status` is snapshotted every cycle into `bikes/date=.../part-*.parquet`, keyed by stable `_bike_number` (the number on the frame). **A bike being ridden is absent from the snapshot** — that absence is the entire basis of trip inference. GBFS has no historical backlog, so a missed cycle is data permanently lost; run it as a service (`deploy/gbfs-poller.service`).

### Trip reconstruction — `analysis/_trips.py` is the single source of truth

`stats.py`, the entire `clip/` pipeline, the deriver and the web import from here so they share one definition of "what a trip is." Change trip semantics **only** in this file (`TRIP_WHERE`, `trips_select()`, `TRIP_CAP`).

- A trip = a bike whose previously-seen *docked* station differs from its currently-seen station (`LAG` over `bikes` partitioned by `bike_number`, sightings without a station ignored), with a gap shorter than `TRIP_CAP` (6 h — longer gaps are treated as the bike being offline, not one ride).
- `build_trips()` is the full offline rebuild (two DuckDB tables, `trips` and `trips_geo` joined to station coordinates with haversine `distance_km` and `duration_min`). It sorts the whole history and needs gigabytes; it is for `stats.py`, the clip and `scripts/check_derived.py`, never for the server.
- `scripts/derive.py` produces the same trips **incrementally** (see "Derived store") from the same `trips_select()`; `check_derived.py` proves both agree on a local store.
- Shared helpers: `partition_dates()` (cheap `date=` listing), `configure_duckdb()` (memory/thread bounds from `DUCKDB_*` env vars), `build_views()`.

### Derived store — `scripts/derive.py` + `analysis/derived.py`

Production never scans the raw streams for the site. A one-shot deriver (run every minute by the `deriver` container, `deploy/derive-loop.sh`) folds the rows the poller wrote since its last pass — selected by `date=` partition and `ingested_at` watermark, one day at a time — into small Parquet files under `derived/`: trips by UTC arrival day, `bike_state`, `obs_latest`, `obs_meta`, `stations_seen`, per-day `station_hourly` sums, plus `meta.json`. All writes are atomic and idempotent; the first pass bootstraps the whole history day by day. Layout and the web's views over it live in `analysis/derived.py`. Design rule for everything here: **minimal RAM on the host first** — disk writes and slower cold requests are acceptable, in-memory materialisation of history is not.
- This is **heuristic and coarse**: endpoints are bounded by the ±5-min poll interval; sub-5-min and return-to-same-station trips are invisible; operator rebalancing (bikes moved by truck) shows up as implausibly fast/long trips — `stats.py` filters these in places, the clip does not.

### `data_root()` and the two possible store locations

The GBFS poller's default `--data-dir` is `data/gbfs/`, but the **on-disk layout in this repo is `data/` directly** (`data/bikes/`, `data/observations/`, `data/stations.parquet`). `_trips.py:data_root()` probes `data/gbfs/` then `data/` and uses whichever has `bikes/` + `stations.parquet`; the web layer's `DATA_DIR` env var overrides it. Rely on `data_root()` (or `web.settings.data_dir()`) rather than hardcoding either path.

### Web layer (`web/`)

FastAPI app serving the dashboard (`web/static/`), per-station pages, and a JSON API. It is a stateless reader: `web/db.py` holds one lock-guarded, memory-bounded DuckDB connection with views over the raw store and over `derived/` (`trips`/`trips_geo`, `obs_latest`, `bike_last`, `obs_meta`, `station_hourly`); nothing is materialized or rebuilt in the process. A background task in `web/app.py` re-creates the views every `LIVE_REFRESH_SEC` so newly appeared files are served; `web/queries.py` is TTL-cached and a cold request pays a small scan of the derived files. Deployment and the memory/latency budget: `deploy/web/README.md`.

### Clip rendering pipeline (`analysis/clip.py` + `analysis/clip/`)

Three stages: reconstruct trips → resolve a routed polyline per OD pair → render frames.

- `clip/osrm.py` — thin OSRM client. Self-hosted bicycle-profile routing on **`localhost:5050`** (5000 is taken by macOS AirPlay). Returns `None` for unroutable pairs (islands/offshore) rather than raising.
- `clip/routes.py` — idempotent per-(origin, destination) route cache at `analysis/out/routes.parquet`. `ensure_routes` fetches only missing pairs; one cache build serves every render window. Geometry stored as JSON `[[lon,lat],...]`.
- `clip/render.py` — matplotlib + ffmpeg. Performance-critical: all trips are pre-flattened into one global segment array, and per frame only a NumPy color/alpha vector is recomputed and swapped into a single `LineCollection` (blit-friendly). Color ramps and alpha are precomputed once. Basemap (CartoDB Dark Matter via contextily) is drawn once.

Outputs and caches under `analysis/out/` and `data/osrm/` are gitignored.

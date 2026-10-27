# StadtRad Hamburg: Open Data Science

Trip reconstruction, statistics, stockout forecasts, and visualizations for
Hamburg's StadtRad bike-share, built on openly available data. Live at
[rad.haw-hamburg.eu](https://rad.haw-hamburg.eu).

A small project for the course *Urbane Mobilität und Elektromobilität*
(summer semester 2026) at the Hamburg University of Applied Sciences
(HAW Hamburg). This code is shipped as-is, without any guarantees
([MIT license](LICENSE)).

<a href="https://loc-history.com/?repos=PaulGoldschmidt/stadtradhamburg&style=sketch">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://loc-history.com/svg?repos=PaulGoldschmidt/stadtradhamburg&style=sketch&theme=dark">
    <source media="(prefers-color-scheme: light)" srcset="https://loc-history.com/svg?repos=PaulGoldschmidt/stadtradhamburg&style=sketch&theme=light">
    <img alt="Lines of code in this repository over time" src="https://loc-history.com/svg?repos=PaulGoldschmidt/stadtradhamburg&style=sketch&theme=light">
  </picture>
</a>

## How it works

- `scripts/gbfs_poller.py` snapshots Deutsche Bahn's Shared-Mobility GBFS API
  every 5 minutes into a partitioned Parquet store. A bike that is being ridden
  is *absent* from a snapshot — that absence is what makes trips reconstructable.
- `analysis/` turns the snapshots into trips (`_trips.py` is the single source
  of truth for what a trip is), stats reports, a forecast, and animated clips.
- `web/` serves the public dashboard and read-only JSON API.

## Collecting data

Deutsche Bahn API-Marketplace credentials go into `.env` at the repo root as
`clientid` / `clientsecret`.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/gbfs_poller.py --once    # one cycle; run as a service for real coverage
```

Store layout (autodetected under `data/` or `data/gbfs/`):

```
data/
  stations.parquet                              # station metadata, refreshed daily
  observations/date=YYYY-MM-DD/part-*.parquet   # per-station status, one file per cycle
  bikes/date=YYYY-MM-DD/part-*.parquet          # per-bike snapshot, every cycle
  weather/                                      # DWD hourly weather (analysis/weather.py)
  state.json                                    # dedup cursors
```

GBFS has no historical backlog — a missed cycle is data lost, so run the poller
as a service (`deploy/gbfs-poller.service`). The first run only seeds cursors
and intentionally writes nothing. (`scripts/stadtrad_poller.py` polled the
city's SensorThings feed, silent since 2026-03-31; kept for reference.)

## Website & API

```sh
pip install -r requirements-web.txt
python -m uvicorn web.app:app --reload --port 8000
```

Dashboard at `/`, per-station pages at `/station/<id>`, forecast at
`/forecast`, API docs at `/docs`. The containerized production stack
(web + poller + forecast) and its update workflow are documented in
[`deploy/web/README.md`](deploy/web/README.md).

## Analysis

```sh
python analysis/stats.py                 # stats report → analysis/out/
jupyter notebook analysis/trips.ipynb    # trip inference + interactive maps
python analysis/forecast.py --help       # stockout forecast; deployment: deploy/forecast.md
```

Trips are heuristic and coarse: endpoints are bounded by the ±5-minute poll
interval, sub-5-minute and same-station round trips are invisible, and operator
rebalancing can look like an implausibly fast ride.

## Trips clip (MP4)

Requires ffmpeg and a local OSRM (bicycle profile):

```sh
bash deploy/osrm/setup.sh
docker compose -f deploy/osrm/docker-compose.yml up -d
python analysis/clip.py --day 2026-06-23 --routes-only   # warm the route cache
python analysis/clip.py --day 2026-06-23                 # render → analysis/out/
```

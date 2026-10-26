# Public website

FastAPI + DuckDB dashboard and read-only JSON API over the Parquet store.

## Run

```sh
# from the repo root — starts the web app, the ingestion poller, the deriver
# and the forecast loop
mkdir -p derived && sudo chown 1000:1000 derived   # once; see "Permissions"
docker compose -f deploy/web/docker-compose.yml up --build -d
# → http://127.0.0.1:9507/   (API docs at /docs; front with a reverse proxy for TLS)
```

The stack has four services sharing one image. It is designed for a minimal
memory footprint on the host: derived data lives in small Parquet files on
disk, heavy work runs in short-lived processes, and every container has a
small hard limit with no swap allowance (`memswap_limit` = `mem_limit`).

| Service | Reads | Writes | What |
|---|---|---|---|
| **web** | `data/`, `derived/`, `analysis/out/` (all read-only) | nothing (DuckDB spill to a scratch volume) | serves the site and the API from the derived files; never rebuilds anything |
| **poller** | DB GBFS API | `data/` | polls Deutsche Bahn GBFS every 5 min and appends new observations/bikes |
| **deriver** | `data/` (read-only) | `derived/` | every minute, one short-lived pass folds what the poller wrote into trips, bike state, latest station status and hourly sums |
| **forecast** | `data/`, `analysis/out/` | `analysis/out/forecast_live.parquet`, `data/weather/` | every 5 min, one short-lived prediction run over the last `LOOKBACK_HOURS` |

The web degrades gracefully: without `derived/` it serves empty history until
the deriver's first pass lands; without `forecast_live.parquet` the map colours
by current occupancy instead of forecast risk.

## Derived store (`derived/`)

`scripts/derive.py` (layout in `analysis/derived.py`) keeps:

- `trips/date=YYYY-MM-DD/trips.parquet` — reconstructed trips by UTC arrival
  day; only the current day's file is rewritten
- `bike_state.parquet`, `obs_latest.parquet`, `obs_meta.parquet`,
  `stations_seen.parquet` — per-bike and per-station state
- `station_hourly/date=YYYY-MM-DD/part.parquet` — (station, local hour) sums
- `meta.json` — watermarks (`ingested_at` of the last processed rows)

Each pass reads only the partitions that can hold new rows, one day at a time,
so memory stays bounded by one day of data however long the store grows. The
first pass on an empty `derived/` replays the whole history the same way (a
few seconds per day of data). Every update is idempotent, so an interrupted
pass is simply redone. The trip definition itself lives in
`analysis/_trips.py` and is shared with the offline `build_trips()`;
`scripts/check_derived.py` proves the two agree on a local store.

Force a rebuild: `rm -rf derived/*` (or just `derived/meta.json` to re-derive
from scratch into the same files). Bump `SCHEMA_VERSION` in
`analysis/derived.py` when the layout changes; the deriver then resets the
store by itself.

## Permissions

The containers run as a non-root user (uid 1000). `data/`, `derived/` and
`analysis/out/` on the host must be writable by that uid for the services that
write there: `sudo chown -R 1000:1000 data derived analysis/out`.

## Live ingestion (poller)

The poller needs DB API credentials in a **`.env` at the repo root** (gitignored):

```
clientid=YOUR_DB_CLIENT_ID
clientsecret=YOUR_DB_API_KEY
```

It appends to the **same** `data/` the web app reads (via its `state.json`
cursor it only writes new data), continuing your existing history. **Do not
also run a host poller** against the same store — one writer only.

Web only (no ingestion, no derivation): `docker compose ... up -d web`.
Logs: `docker compose -f deploy/web/docker-compose.yml logs -f poller deriver`.

## Live forecast (forecast service)

The `forecast` service reruns
`analysis/forecast.py --predict --horizons "$HORIZONS" --lookback-hours "$LOOKBACK_HOURS"`
every `FORECAST_INTERVAL` seconds and writes `analysis/out/forecast_live.parquet`,
which the web app picks up automatically. Each run reads only the last
`LOOKBACK_HOURS` (default 24) of observations plus the outage cap, uses the
per-station capacity stored in the model bundles, and loads one bundle at a
time. Stations silent for longer than the window get no forecast. Before
starting it:

- **Transfer the trained bundles** `forecast_model_h{30,60,90,120}.joblib` into the
  host `analysis/out/` (they're gitignored; train with
  `for h in 30 60 90 120; do .venv/bin/python analysis/forecast.py --save-model --horizon-min $h; done`).
  Stations added after training get a capacity estimated from the window and
  are listed in the forecast log; retrain off-box now and then to refresh.

To retrain the models (not just re-predict), do it off-box and re-transfer the
bundles — the loop only *predicts*. Forecast logs:
`docker compose -f deploy/web/docker-compose.yml logs -f forecast`.

## Update a deployment

```sh
docker compose -f deploy/web/docker-compose.yml down
git pull
docker compose -f deploy/web/docker-compose.yml up --build -d
```

`derived/` survives updates and is reused; only a `SCHEMA_VERSION` bump
triggers a re-derivation.

## Local dev (no Docker)

```sh
.venv/bin/python scripts/derive.py                      # build/refresh derived/
.venv/bin/python -m uvicorn web.app:app --reload --port 8000
.venv/bin/python scripts/smoke_web.py                   # every endpoint, cold + warm, RSS
.venv/bin/python scripts/check_derived.py               # derived/ == full rebuild?
```

## Budget (memory first, latency second)

Measured on a laptop with a 36-day store (35.8 M bike sightings, 231 k trips),
2026-10-26. These are the numbers the deployment accepts; do not "optimise"
them back into memory.

| | |
|---|---|
| web process RSS | ~180 MB at startup, ~300 MB after serving every endpoint (128 MB DuckDB buffer pool) |
| web latency, cold cache | `/api/stats/today` ~600 ms, history endpoints 50–170 ms, station page ~220 ms |
| web latency, warm cache | under 5 ms for everything |
| deriver pass, nothing new | ~65 MB RSS, well under a second |
| deriver pass, one poll cycle | ~0.4 s |
| deriver bootstrap | ~350 MB RSS peak, ~0.8 s per day of data |
| forecast run | ~2–3 s, 420–470 MB RSS peak (import baseline ~220 MB); 1.7 GB and rising with history before |
| full offline rebuild (`check_derived.py`, not run on the host) | 4.7 GB RSS |

## Notes

- **Config** via env (see `web/settings.py`): `DATA_DIR`, `DERIVED_DIR`,
  `DUCKDB_THREADS`, `DUCKDB_MEMORY_LIMIT`, `DUCKDB_TEMP_DIR`,
  `LIVE_REFRESH_SEC`, TTLs, rate limits, `CORS_ORIGINS`.
- **Rate limits** (per IP, `slowapi`): global `RATE_GLOBAL`, history
  `RATE_HISTORY`, downloads `RATE_DOWNLOAD` → HTTP 429 over the cap.
- **CPU stays flat under traffic**: every query is TTL-cached, `DUCKDB_THREADS`
  bounds per-query CPU, and the containers are capped with `cpus`.
- For public exposure, front with nginx/Caddy for TLS and set `CORS_ORIGINS`.

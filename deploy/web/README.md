# Public website (yay)

FastAPI + DuckDB dashboard and read-only JSON API over the Parquet store.

## Run

```sh
# from the repo root — starts the web app AND the ingestion poller
docker compose -f deploy/web/docker-compose.yml up --build -d
# → http://127.0.0.1:9507/   (API docs at /docs; front with a reverse proxy for TLS)
```

The stack has two services sharing one image and one `data/` store:

- **web** — reads `data/` and `analysis/out/` **read-only** and serves the site.
  Degrades gracefully if `forecast_live.parquet` is absent (map colours by
  current occupancy instead of forecast risk).
- **poller** — writes `data/` **read-write**: polls Deutsche Bahn GBFS every
  5 min and appends new observations/bikes, so the site auto-updates.

## Live ingestion (poller)

The poller needs DB API credentials in a **`.env` at the repo root** (gitignored):

```
clientid=YOUR_DB_CLIENT_ID
clientsecret=YOUR_DB_API_KEY
```

It appends to the **same** `data/` the web app reads (via its `state.json`
cursor it only writes new data), continuing your existing history. Two caveats:

- **Do not also run a host poller** against the same store — one writer only.
- The container runs as a non-root user, so the host `data/` dir must be
  **writable by that uid** (e.g. `sudo chown -R 1000:1000 data/`), or the poller
  can't write. The web service is read-only and unaffected.

Web only (no ingestion): `docker compose ... up -d web`.
Poller logs: `docker compose -f deploy/web/docker-compose.yml logs -f poller`.

## Local dev (no Docker)

```sh
.venv/bin/python -m uvicorn web.app:app --reload --port 8000
```

## Notes

- **CPU stays flat under traffic** via three layers: the expensive trip
  reconstruction runs once at startup + every `TRIP_REFRESH_SEC`; every query is
  TTL-cached; `DUCKDB_THREADS` bounds per-query CPU; and the container is capped
  at `cpus: 1.0` / `mem_limit: 512m`.
- **Rate limits** (per IP, `slowapi`): global `RATE_GLOBAL`, history
  `RATE_HISTORY`, downloads `RATE_DOWNLOAD` → HTTP 429 over the cap.
- **Config** via env (see `web/settings.py`): `DATA_DIR`, `CORS_ORIGINS`, TTLs,
  rate limits, `DUCKDB_THREADS`.
- For public exposure, front with nginx/Caddy for TLS and set `CORS_ORIGINS`.
- Data grows over months → the startup/refresh trip rebuild scans all of
  `bikes/`; bound it to recent `date=` partitions later (same note as the
  forecast `--predict` scaling caveat).
```

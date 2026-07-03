# Public website (yay)

FastAPI + DuckDB dashboard and read-only JSON API over the Parquet store.

## Run

```sh
# from the repo root
docker compose -f deploy/web/docker-compose.yml up --build -d
# → http://<host>:8000/   (API docs at /docs)
```

Mounts `data/` (poller output) and `analysis/out/` (the `forecast_live.parquet`
the forecast timer writes) **read-only**. The site degrades gracefully if the
forecast file is absent (map colours by current occupancy instead of risk).

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

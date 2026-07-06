"""FastAPI app: curated JSON API + static single-page dashboard.

Startup warms the DuckDB connection, materialized tables, and response caches; a
background task then refreshes the current-status table every LIVE_REFRESH_SEC
and does the full trips rebuild every TRIP_REFRESH_SEC, re-warming the response
caches after each — so requests are always served from memory and never pay a
Parquet scan. All query work runs in a threadpool (the DuckDB access itself is
lock-serialized) and is TTL-cached. Per-IP rate limits via slowapi. See
deploy/web/ for the container CPU/memory limits.
"""

from __future__ import annotations

import asyncio
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.concurrency import run_in_threadpool

from . import db, queries, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("web")

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.RATE_GLOBAL])
STATIC = Path(__file__).resolve().parent / "static"


async def _refresher() -> None:
    """Cheap current-status refresh every LIVE_REFRESH_SEC; full trips rebuild
    every TRIP_REFRESH_SEC. Each cycle re-warms the affected response caches."""
    period = settings.LIVE_REFRESH_SEC
    heavy_every = max(1, round(settings.TRIP_REFRESH_SEC / period))
    cycle = 0
    while True:
        await asyncio.sleep(period)
        cycle += 1
        try:
            if cycle % heavy_every == 0:
                await run_in_threadpool(db.refresh_trips)
                await run_in_threadpool(queries.warm_history)
            else:
                await run_in_threadpool(db.refresh_live)
            await run_in_threadpool(queries.warm_live)
        except Exception:
            log.exception("background refresh failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await run_in_threadpool(db.init)
    try:
        await run_in_threadpool(queries.warm_history)
        await run_in_threadpool(queries.warm_live)
    except Exception:
        log.exception("startup cache warm failed")
    task = asyncio.create_task(_refresher())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(
    title="StadtRad Hamburg — Open Data",
    description="Public read-only API + dashboard for Hamburg bike-share utilization, "
                "forecasts, and history. Data: Deutsche Bahn GBFS + DWD (Bright Sky).",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_middleware(
    CORSMiddleware, allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET"], allow_headers=["*"],
)


@app.exception_handler(RateLimitExceeded)
async def _ratelimit_handler(request: Request, exc: RateLimitExceeded):
    from slowapi import _rate_limit_exceeded_handler
    return _rate_limit_exceeded_handler(request, exc)


# ---------- live ----------------------------------------------------------------

@app.get("/api/health", tags=["live"])
async def health(request: Request):
    return await run_in_threadpool(queries.health)


@app.get("/api/stats/today", tags=["live"])
async def stats_today(request: Request):
    return await run_in_threadpool(queries.stats_today)


@app.get("/api/stations", tags=["live"])
async def stations(request: Request):
    return await run_in_threadpool(queries.stations_live)


# ---------- history -------------------------------------------------------------

@app.get("/api/history/summary", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def history_summary(request: Request):
    return await run_in_threadpool(queries.history_summary)


@app.get("/api/history/trips_daily", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def trips_daily(request: Request):
    return await run_in_threadpool(queries.trips_daily)


@app.get("/api/history/heatmap", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def heatmap(request: Request):
    return await run_in_threadpool(queries.heatmap)


@app.get("/api/history/top_stations", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def top_stations(request: Request):
    return await run_in_threadpool(queries.top_stations)


@app.get("/api/history/bikes", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def bike_usage(request: Request):
    return await run_in_threadpool(queries.bike_usage)


@app.get("/api/weather/ridership", tags=["history"])
@limiter.limit(settings.RATE_HISTORY)
async def weather_ridership(request: Request):
    return await run_in_threadpool(queries.weather_ridership)


# ---------- forecast ------------------------------------------------------------

@app.get("/api/forecast/horizons", tags=["forecast"])
async def forecast_horizons(request: Request):
    return await run_in_threadpool(queries.forecast_horizons)


@app.get("/api/forecast", tags=["forecast"])
async def forecast(request: Request, horizon: int):
    meta = await run_in_threadpool(queries.forecast_horizons)
    if not meta["available"]:
        raise HTTPException(status_code=404, detail="no forecast available")
    if meta["horizons"] and horizon not in meta["horizons"]:
        raise HTTPException(
            status_code=400,
            detail=f"horizon must be one of {meta['horizons']} minutes",
        )
    return await run_in_threadpool(queries.forecast_at, horizon)


# ---------- per-station ---------------------------------------------------------

@app.get("/api/station/{station_id}", tags=["station"])
async def station_detail(request: Request, station_id: str):
    data = await run_in_threadpool(queries.station_detail, station_id)
    if not data:
        raise HTTPException(status_code=404, detail="unknown station")
    return data


@app.get("/api/station/{station_id}/insights", tags=["station"])
async def station_insights(request: Request, station_id: str):
    data = await run_in_threadpool(queries.station_insights, station_id)
    if not data:
        raise HTTPException(status_code=404, detail="unknown station")
    return data


# ---------- downloads (curated aggregates) --------------------------------------

@app.get("/api/download/{filename}", tags=["download"])
@limiter.limit(settings.RATE_DOWNLOAD)
async def download(request: Request, filename: str):
    name, _, fmt = filename.rpartition(".")
    if name not in queries.DOWNLOADS:
        raise HTTPException(status_code=404, detail=f"unknown dataset; choose one of {queries.DOWNLOADS}")
    if fmt not in ("csv", "parquet"):
        raise HTTPException(status_code=400, detail="extension must be .csv or .parquet")
    df = await run_in_threadpool(queries.download_frame, name)
    if fmt == "csv":
        buf = io.StringIO()
        df.to_csv(buf, index=False)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}.parquet"'},
    )


# Assets under /static; the dashboard shell is served at the root.
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/forecast", include_in_schema=False)
async def forecast_page():
    return FileResponse(STATIC / "forecast.html")


@app.get("/station/{station_id}", include_in_schema=False)
async def station_page(station_id: str):
    # The shell is static; station.js reads the id from the URL path.
    return FileResponse(STATIC / "station.html")

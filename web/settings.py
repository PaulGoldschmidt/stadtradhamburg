"""Environment-driven configuration. All knobs have safe defaults so the app runs
with zero config in dev and is tunable via env vars in the container."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Reuse the analysis layer (_trips, weather) as the single source of truth.
sys.path.insert(0, str(REPO / "analysis"))

LOCAL_TZ = "Europe/Berlin"
FORECAST_LIVE = REPO / "analysis" / "out" / "forecast_live.parquet"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def data_dir() -> Path:
    """Parquet store location. DATA_DIR overrides the analysis autodetect."""
    override = os.environ.get("DATA_DIR")
    if override:
        return Path(override)
    from _trips import data_root
    return data_root()


# DuckDB CPU bound + cache lifetimes (seconds).
DUCKDB_THREADS = _int("DUCKDB_THREADS", 2)
TRIP_REFRESH_SEC = _int("TRIP_REFRESH_SEC", 300)
TTL_LIVE = _int("TTL_LIVE", 60)
TTL_HISTORY = _int("TTL_HISTORY", 3600)
TTL_STATION = _int("TTL_STATION", 300)

# Per-IP rate limits (slowapi syntax).
RATE_GLOBAL = os.environ.get("RATE_GLOBAL", "120/minute")
RATE_HISTORY = os.environ.get("RATE_HISTORY", "30/minute")
RATE_DOWNLOAD = os.environ.get("RATE_DOWNLOAD", "6/minute")

# CORS: comma-separated origins, or "*".
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")]

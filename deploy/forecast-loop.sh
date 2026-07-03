#!/usr/bin/env bash
# In-container forecast loop: every FORECAST_INTERVAL seconds, backfill weather and
# regenerate analysis/out/forecast_live.parquet (the report the web app reads).
# Runs as the `forecast` service in deploy/web/docker-compose.yml.
#
# Requires the per-horizon model bundles (forecast_model_h*.joblib) to be present
# in the mounted analysis/out/ — they are gitignored, so transfer them once.
# `--no-map` skips the folium map (not installed in this image; the web renders its
# own map). A failed cycle is logged and retried next tick — never fatal.
set -uo pipefail
cd /app

INTERVAL="${FORECAST_INTERVAL:-300}"
HORIZONS="${HORIZONS:-30 60 90 120}"

echo "forecast-loop: every ${INTERVAL}s, horizons: ${HORIZONS}"
while true; do
  python analysis/weather.py --log-level WARNING || echo "forecast-loop: weather refresh failed"
  python analysis/forecast.py --predict --horizons $HORIZONS --no-map || echo "forecast-loop: predict failed"
  sleep "$INTERVAL"
done

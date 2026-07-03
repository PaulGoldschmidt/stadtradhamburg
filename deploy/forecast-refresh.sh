#!/usr/bin/env bash
# Refresh the live StadtRad utilization forecast + map. Idempotent; cron/timer-safe.
#
# Steps:
#   1. backfill observed DWD weather up to "now" (the window auto-extends as the
#      poller appends new observations)
#   2. run the forecast in serving mode — this also tops up *forecast* weather for
#      the target hour and renders the page at analysis/out/site/index.html
#
# Prerequisite: a trained model bundle must already exist. Create one once with:
#   .venv/bin/python analysis/forecast.py --save-model --horizon-min "${HORIZON_MIN:-60}"
#
# Env overrides: PYTHON (interpreter), HORIZON_MIN (must match the saved bundle).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
HORIZON_MIN="${HORIZON_MIN:-60}"

"$PY" analysis/weather.py --log-level WARNING
"$PY" analysis/forecast.py --predict --horizon-min "$HORIZON_MIN"

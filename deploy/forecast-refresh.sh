#!/usr/bin/env bash
# Refresh the live StadtRad utilization forecast + map. Idempotent; cron/timer-safe.
#
# Steps:
#   1. backfill observed DWD weather up to "now" (the window auto-extends as the
#      poller appends new observations)
#   2. run the forecast in serving mode — this also tops up *forecast* weather for
#      the target hour and renders the page at analysis/out/site/index.html
#
# Prerequisite: a trained model bundle per horizon must already exist. Create them
# once with (repeat for each horizon in HORIZONS):
#   for h in 30 60 90 120; do .venv/bin/python analysis/forecast.py --save-model --horizon-min $h; done
#
# Env overrides: PYTHON (interpreter), HORIZONS (space-separated minutes; each needs a bundle).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-.venv/bin/python}"
HORIZONS="${HORIZONS:-30 60 90 120}"

"$PY" analysis/weather.py --log-level WARNING
"$PY" analysis/forecast.py --predict --horizons $HORIZONS

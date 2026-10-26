#!/usr/bin/env bash
# In-container derivation loop: every DERIVE_INTERVAL seconds run one pass of
# scripts/derive.py, which folds whatever the poller wrote since the last pass
# into derived/ (trips, bike state, latest station status, hourly sums). Runs as
# the `deriver` service in deploy/web/docker-compose.yml.
#
# Each pass is a separate short-lived process, so no memory is held between
# passes; an idle pass (nothing new) costs a directory listing. A failed pass is
# logged and retried next tick — never fatal. The first pass on an empty
# derived/ bootstraps from the full history, one partition day at a time.
set -uo pipefail
cd /app

INTERVAL="${DERIVE_INTERVAL:-60}"

echo "derive-loop: every ${INTERVAL}s"
while true; do
  python scripts/derive.py --log-level "${DERIVE_LOG_LEVEL:-INFO}" || echo "derive-loop: pass failed"
  sleep "$INTERVAL"
done

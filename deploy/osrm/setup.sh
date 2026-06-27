#!/usr/bin/env bash
# Build a Hamburg bicycle-profile OSRM graph. One-shot prep; run again only to
# refresh the OSM extract.
set -euo pipefail

cd "$(dirname "$0")/../../data/osrm" 2>/dev/null || {
  mkdir -p "$(dirname "$0")/../../data/osrm"
  cd "$(dirname "$0")/../../data/osrm"
}

PBF=hamburg-latest.osm.pbf
URL=https://download.geofabrik.de/europe/germany/hamburg-latest.osm.pbf
IMAGE=ghcr.io/project-osrm/osrm-backend:latest

if [[ ! -f "$PBF" ]]; then
  echo "==> downloading $PBF (~80 MB) from Geofabrik"
  curl -fSL --progress-bar "$URL" -o "$PBF"
else
  echo "==> $PBF already present, skipping download"
fi

docker pull "$IMAGE"

echo "==> osrm-extract (bicycle profile)"
docker run --rm -v "$PWD:/data" "$IMAGE" \
  osrm-extract -p /opt/bicycle.lua "/data/$PBF"

echo "==> osrm-partition"
docker run --rm -v "$PWD:/data" "$IMAGE" \
  osrm-partition "/data/hamburg-latest.osrm"

echo "==> osrm-customize"
docker run --rm -v "$PWD:/data" "$IMAGE" \
  osrm-customize "/data/hamburg-latest.osrm"

echo
echo "==> Graph built. Start the routing service with:"
echo "      docker compose -f deploy/osrm/docker-compose.yml up -d"
echo "    Smoke test:"
echo "      curl 'http://localhost:5050/route/v1/cycling/9.99,53.55;10.01,53.55?overview=full'"

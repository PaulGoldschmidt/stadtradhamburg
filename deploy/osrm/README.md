# OSRM — local cycling routing for Hamburg

Used by `analysis/clip.py` to turn ~28 k unique `(origin, destination)` station
pairs into road-following polylines. One-time setup, then sub-millisecond
routing for the lifetime of the cache.

## Prerequisites

- Docker Desktop (or any Docker daemon)
- ~500 MB free disk (PBF + indexes live in `data/osrm/`)

## Setup (one-shot)

```sh
bash deploy/osrm/setup.sh
docker compose -f deploy/osrm/docker-compose.yml up -d
```

The setup script downloads `hamburg-latest.osm.pbf` from Geofabrik, then runs
`osrm-extract → osrm-partition → osrm-customize` with the bundled
`/opt/bicycle.lua` profile. Total wall-clock: ~3-5 minutes on a recent Mac.

## Verify

```sh
curl 'http://localhost:5050/route/v1/cycling/9.99,53.55;10.01,53.55?overview=full'
```

Expect a JSON response with `routes[0].geometry` (polyline-encoded) and
`routes[0].distance` in metres.

## Lifecycle

- `docker compose -f deploy/osrm/docker-compose.yml down` — stop the routing
  service (route cache stays valid; nothing in Parquet needs re-fetching)
- `bash deploy/osrm/setup.sh` — re-run only to refresh the OSM extract

`data/osrm/` is gitignored.

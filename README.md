# Stadtrad Hamburg: Open Data Science
Data Science with open data from the Stadtrad Hamburg Portal.
Live Website hosted at [rad.haw-hamburg.eu](https://rad.haw-hamburg.eu)

## Running the poller 
`scripts/stadtrad_poller.py` polls that API on a 5-minute cadence and appends only-new observations to a partitioned Parquet store under `data/`.

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# one cycle (useful for cron / smoke test)
python scripts/stadtrad_poller.py --once

# run as a daemon
python scripts/stadtrad_poller.py
```

Output layout:

```
data/
  datastreams.parquet                          # station metadata, refreshed daily
  observations/date=YYYY-MM-DD/part-*.parquet  # one file per poll cycle
  state.json                                   # per-station last phenomenonTime cursor
```

Read it back with pyarrow / DuckDB:

```python
import pyarrow.dataset as ds
ds.dataset("data/observations", partitioning="hive").to_table()
```

The **first** run only seeds cursors — it intentionally writes no observations,
so the dataset only contains data from the moment the poller started.

## Alternative source: GBFS via Deutsche Bahn

The Hamburg SensorThings feed has been silent since 2026-03-31. As a fallback,
`scripts/gbfs_poller.py` polls the same StadtRad data via Deutsche Bahn's
Shared-Mobility GBFS API (`apis.deutschebahn.com`). It writes to a separate
`data/gbfs/` tree:

```
data/gbfs/
  stations.parquet                                     # station metadata (daily refresh)
  observations/date=YYYY-MM-DD/part-*.parquet          # per-station status (bikes/docks available)
  bikes/date=YYYY-MM-DD/part-*.parquet                 # per-bike snapshot, every cycle
  state.json                                           # per-station last_reported cursor
```

Credentials are loaded from `.env` at the repo root (keys `clientid` and
`clientsecret` — both required). Usage is otherwise identical:

```sh
python scripts/gbfs_poller.py --once       # smoke test
python scripts/gbfs_poller.py              # daemon
```

GBFS returns only the *current* snapshot; there is no historical backlog, so a
missed cycle is data lost. Run as a service for continuous coverage — see
`deploy/gbfs-poller.service`.

### Inferring rentals from `bikes/`

Each row in `bikes/` is one visible bike in one poll cycle, keyed by the stable
`bike_number` (e.g. `"30277"` — the number printed on the frame). A bike that
is currently being ridden is *absent* from the snapshot; it reappears at the
destination once parked. The diff between consecutive snapshots therefore
yields rentals: bike X last seen at station A at T1, first seen at station B at
T2 → one rental from A to B between T1 and T2.

DuckDB starter (run once a few hours of data have accumulated):

```python
import duckdb
con = duckdb.connect()
con.execute("CREATE VIEW b AS SELECT * FROM 'data/gbfs/bikes/date=*/part-*.parquet'")
con.execute("""
  WITH ordered AS (
    SELECT bike_number, station_id, observed_at,
           LAG(station_id)  OVER w AS prev_station,
           LAG(observed_at) OVER w AS prev_seen
    FROM b WHERE station_id IS NOT NULL
    WINDOW w AS (PARTITION BY bike_number ORDER BY observed_at)
  )
  SELECT bike_number, prev_station AS origin, station_id AS destination,
         prev_seen AS departed_after, observed_at AS arrived_by
  FROM ordered
  WHERE prev_station IS NOT NULL AND prev_station <> station_id
  ORDER BY arrived_by
""").df().head()
```

Trip endpoints are bounded by the poll interval (±5 min by default), so
duration estimates are coarse. Sub-5-minute trips and return-to-same-station
trips are invisible.

See `analysis/trips.ipynb` for an end-to-end trip-inference and visualization
pipeline (timeseries, top stations, duration histogram, interactive OD map),
and `analysis/stats.py` for extended descriptive statistics (outliers, net
flow, occupancy).

## Rendering a trips clip

`analysis/clip.py` produces a dark-theme MP4 of trips flowing across Hamburg,
with road-following routes provided by a local OSRM service. One-time setup:

```sh
brew install ffmpeg                                 # macOS; otherwise apt/dnf
bash deploy/osrm/setup.sh                           # builds the OSRM graph
docker compose -f deploy/osrm/docker-compose.yml up -d
```

Then:

```sh
# Warm the per-OD-pair route cache (re-runs are no-ops)
python analysis/clip.py --day 2026-06-23 --routes-only

# Render one day, 1 sec = 5 min real
python analysis/clip.py --day 2026-06-23

# Render a single evening peak (quick smoke test)
python analysis/clip.py --day 2026-06-23 --start-hour 17 --end-hour 18 \
  --time-scale 60 --fps 30

# Full 29-day hyperlapse, 1 sec = 1 h real
python analysis/clip.py --start 2026-05-29 --end 2026-06-26 --time-scale 3600
```

Output goes to `analysis/out/clip_<window>.mp4`. The route cache
(`analysis/out/routes.parquet`) and OSM extract (`data/osrm/`) are gitignored.


### Workflow

1. `docker compose down`
2. `git pull`
3. `docker compose up --build -d`
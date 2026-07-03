# Live forecast deployment

Runs the bike-utilization forecast on a cadence and publishes an interactive map.

Pieces:

- `forecast-refresh.sh` — one idempotent refresh: backfill observed weather →
  `forecast.py --predict` (forecast-weather top-up + predictions + map).
- `forecast.service` + `forecast.timer` — run the refresh every 5 min.
- `forecast-web.service` — optional static server for `analysis/out/site/`.

## One-time setup

The poller (`gbfs-poller.service`) must be running so observations stay current,
and the analysis scripts must find the data under `<repo>/data` (symlink if your
poller writes elsewhere — see the note in `forecast.service`).

```sh
# train + persist the model bundle the refresh serves from (pick your horizon)
.venv/bin/python analysis/forecast.py --save-model --horizon-min 60
```

## Local / quick

```sh
deploy/forecast-refresh.sh                       # generate once
python -m http.server -d analysis/out/site 8000  # open http://localhost:8000/
```

## systemd

```sh
sudo cp deploy/forecast.service deploy/forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now forecast.timer       # periodic regeneration
sudo systemctl start forecast.service            # run once now
# optional always-on static site:
sudo cp deploy/forecast-web.service /etc/systemd/system/
sudo systemctl enable --now forecast-web.service
```

`HORIZON_MIN` in `forecast.service` must match the horizon the bundle was trained
with. Logs: `journalctl -u forecast.service -f`.

## Publishing the page

`analysis/out/site/index.html` is a self-contained Leaflet page (no backend). Host
it however you like: `http.server`/nginx/Caddy, or push the dir to GitHub Pages or
object storage after each refresh.

## Scaling note

`--predict` currently rebuilds the panel over **all** history each run (a few
seconds on ~1 month of data). Since `observations/` is partitioned by `date=`,
a long-running deployment should bound the serving read to the last day or two of
partitions — the model only needs ~1 h of lookback for its features.

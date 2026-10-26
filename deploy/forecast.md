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
# train + persist one bundle per horizon the Forecast page offers
for h in 30 60 90 120; do
  .venv/bin/python analysis/forecast.py --save-model --horizon-min $h
done
```

The refresh then predicts all horizons at once (`--predict --horizons 30 60 90 120`)
into a single long-format `forecast_live.parquet`; the website's `/forecast` page
lets users pick which horizon to view.

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

`--predict` reads only the `date=` partitions that can matter for the last
`--lookback-hours` (default 48; the container uses 24) plus the outage cap,
takes the per-station capacity from the model bundle instead of a full-history
quantile, and loads one bundle at a time, so a run stays at a few seconds and a
few hundred MB however long the store grows. Stations silent for longer than
the window get no forecast.

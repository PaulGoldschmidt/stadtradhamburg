"""Forecast bike utilization per station, to help the operator pre-empt
stockouts (empty stations strand riders) and full-outs (no docks to return to).

Pipeline (all the data-specific traps are handled here — see comments):
  1. Resample the change-log `observations` onto a regular per-station grid via
     an ASOF join (forward-fill), capping fill across outages.
  2. Infer each station's *effective* capacity from bikes+docks — the reported
     `capacity` is a placeholder (every station claims 40).
  3. Engineer features: recent levels/flow (lags, rolling, deltas), calendar
     (cyclical, local-time), Hamburg weather joined by hour, and a train-only
     (station, weekday, hour) occupancy prior.
  4. Split by TIME (never randomly — lag features would leak), train one global
     LightGBM across all stations for the chosen horizon, and score it against
     persistence + seasonal-naive baselines it must beat.

Targets at horizon h:
  - regression:     num_bikes_available at t+h, modeled as the *change* from the
                    current level (Δ-target) so it can't underperform persistence
  - classification: P(empty at t+h) i.e. bikes==0      (PR-AUC, recall) ← key one

Usage:
    .venv/bin/python analysis/forecast.py                       # train + evaluate, 60-min horizon
    .venv/bin/python analysis/forecast.py --horizon-min 30 --step-min 10
    .venv/bin/python analysis/forecast.py --save-model          # also persist a model for serving
    .venv/bin/python analysis/forecast.py --predict --horizons 30 60 90 120   # serve multi-horizon
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _trips import LOCAL_TZ, data_root  # noqa: E402
from weather import (  # noqa: E402
    HAMBURG_LAT, HAMBURG_LON, build_weather_view, weather_path,
    run as fetch_weather,
)

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "analysis" / "out"
SITE = OUT / "site"  # standalone web page(s): serve this dir as the website

CAP_QUANTILE = 0.99  # effective capacity = this quantile of (bikes + docks)


# ---------- panel construction --------------------------------------------------

def build_panel(
    con: duckdb.DuckDBPyConnection,
    data_dir: Path,
    step_min: int,
    outage_cap_hours: int,
    lookback_hours: int | None = None,
) -> None:
    """Resample observations onto a regular `step_min` grid per station.

    The poller writes only state *changes*, so the raw series is irregular. We
    build a dense (station × time) grid and ASOF-join the last-known state onto
    each slot (forward-fill). Slots whose newest obs is older than the outage
    cap are nulled — we don't fill bikes across a multi-hour data gap.

    `lookback_hours` bounds the grid to the most recent window (serving only needs
    the latest slot per station, so a small window keeps memory/CPU flat as history
    grows). `None` = full history (training). The ASOF fill still reads all obs, so
    forward-fill into the window start is correct."""
    obs_glob = data_dir / "observations" / "date=*" / "part-*.parquet"
    con.execute(f"CREATE OR REPLACE VIEW obs_raw AS SELECT * FROM '{obs_glob}'")

    # Effective capacity per station (the reported `capacity` is a flat 40).
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE cap AS
        SELECT station_id,
               GREATEST(quantile_cont(num_bikes_available + num_docks_available,
                                       {CAP_QUANTILE}), 1) AS capacity_eff
        FROM obs_raw
        WHERE is_installed AND num_docks_available IS NOT NULL
        GROUP BY 1
        """
    )

    # Regular time grid, snapped to the step. generate_series is inclusive.
    start_expr = "(SELECT t0 FROM bounds)"
    if lookback_hours is not None:
        start_expr = (f"GREATEST((SELECT t0 FROM bounds), "
                      f"(SELECT t1 FROM bounds) - INTERVAL {int(lookback_hours)} HOUR)")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE grid AS
        WITH bounds AS (
          SELECT time_bucket(INTERVAL {step_min} MINUTE, min(last_reported)) AS t0,
                 time_bucket(INTERVAL {step_min} MINUTE, max(last_reported)) AS t1
          FROM obs_raw
        )
        SELECT s.station_id, g.t
        FROM (SELECT DISTINCT station_id FROM cap) s
        CROSS JOIN (
          SELECT unnest(generate_series(
                   {start_expr},
                   (SELECT t1 FROM bounds),
                   INTERVAL {step_min} MINUTE)) AS t
        ) g
        """
    )

    # ASOF forward-fill: most recent obs at or before each grid slot.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE panel AS
        SELECT g.station_id, g.t,
               c.capacity_eff,
               o.num_bikes_available AS bikes_raw,
               o.num_docks_available AS docks_raw,
               o.is_installed,
               date_diff('minute', o.last_reported, g.t) AS staleness_min
        FROM grid g
        JOIN cap c USING (station_id)
        ASOF LEFT JOIN obs_raw o
          ON g.station_id = o.station_id AND g.t >= o.last_reported
        """
    )

    # Null out values filled across an outage; keep only installed slots.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE panel AS
        SELECT station_id, t, capacity_eff, staleness_min,
               CASE WHEN staleness_min <= {outage_cap_hours * 60} THEN bikes_raw END AS bikes,
               CASE WHEN staleness_min <= {outage_cap_hours * 60} THEN docks_raw END AS docks,
               COALESCE(is_installed, FALSE) AS is_installed
        FROM panel
        WHERE COALESCE(is_installed, FALSE)
        """
    )


def build_feature_frame(
    con: duckdb.DuckDBPyConnection,
    horizon_steps: int,
    step_min: int,
    use_weather: bool,
) -> pd.DataFrame:
    """Window-function feature build over the panel; returns a pandas frame.

    Lags/rolling are in grid steps. The target time (t + horizon) is a known
    future clock time, so its calendar features are legitimate inputs."""
    s1 = max(1, 15 // step_min)    # ~15 min
    s2 = max(1, 30 // step_min)    # ~30 min
    s4 = max(1, 60 // step_min)    # ~60 min

    # Join weather at the TARGET hour (t+h), not the current hour: that hour is
    # observed in the training history but a *forecast* at serving time, which is
    # exactly the train/serve swap the serving path relies on. It's also the
    # better feature — weather during the prediction window drives the demand.
    weather_join = "LEFT JOIN weather w ON w.timestamp = date_trunc('hour', f.t_target)" if use_weather else ""
    weather_cols = (
        "w.temperature, w.precipitation, w.wind_speed, w.relative_humidity, "
        "w.cloud_cover, (w.condition = 'rain')::INT AS is_rain,"
        if use_weather
        else "CAST(NULL AS DOUBLE) AS temperature, CAST(NULL AS DOUBLE) AS precipitation, "
             "CAST(NULL AS DOUBLE) AS wind_speed, CAST(NULL AS DOUBLE) AS relative_humidity, "
             "CAST(NULL AS DOUBLE) AS cloud_cover, CAST(NULL AS INT) AS is_rain,"
    )

    sql = f"""
    WITH f AS (
      SELECT
        station_id, t, capacity_eff, staleness_min,
        bikes, docks,
        bikes::DOUBLE / capacity_eff                              AS occ,
        -- recent levels
        lag(bikes, {s1}) OVER w                                   AS bikes_lag1,
        lag(bikes, {s2}) OVER w                                   AS bikes_lag2,
        lag(bikes, {s4}) OVER w                                   AS bikes_lag4,
        -- recent flow (net change over the last 15/60 min)
        bikes - lag(bikes, {s1}) OVER w                           AS flow_15,
        bikes - lag(bikes, {s4}) OVER w                           AS flow_60,
        -- rolling level over the last hour
        avg(bikes)  OVER w1h                                      AS roll_mean_1h,
        stddev_pop(bikes) OVER w1h                                AS roll_std_1h,
        -- TARGETS at t + horizon
        lead(bikes, {horizon_steps}) OVER w                       AS y_bikes,
        (lead(bikes, {horizon_steps}) OVER w = 0)::INT            AS y_empty,
        (lead(docks, {horizon_steps}) OVER w = 0)::INT            AS y_full,
        -- target clock time (known in advance → legit feature)
        p_t_target.t_target                                       AS t_target
      FROM panel p,
           LATERAL (SELECT p.t + INTERVAL {horizon_steps * step_min} MINUTE AS t_target) p_t_target
      WINDOW w   AS (PARTITION BY station_id ORDER BY t),
             w1h AS (PARTITION BY station_id ORDER BY t
                     ROWS BETWEEN {s4} PRECEDING AND CURRENT ROW)
    )
    SELECT f.*,
           -- calendar features for the CURRENT time (local)
           extract('hour'  FROM t AT TIME ZONE '{LOCAL_TZ}')::INT      AS hour,
           extract('dow'   FROM t AT TIME ZONE '{LOCAL_TZ}')::INT      AS dow,
           -- calendar features for the TARGET time (local)
           extract('hour' FROM t_target AT TIME ZONE '{LOCAL_TZ}')::INT AS hour_tgt,
           extract('dow'  FROM t_target AT TIME ZONE '{LOCAL_TZ}')::INT AS dow_tgt,
           {weather_cols}
           capacity_eff AS capacity
    FROM f
    {weather_join}
    """
    df = con.execute(sql).df()

    # Cyclical encodings (sin/cos) for hour-of-day, current and target.
    for col in ("hour", "hour_tgt"):
        rad = 2 * np.pi * df[col] / 24.0
        df[f"{col}_sin"] = np.sin(rad)
        df[f"{col}_cos"] = np.cos(rad)
    df["is_weekend"] = (df["dow_tgt"] >= 5).astype(int)
    return df


# ---------- split, prior, model -------------------------------------------------

def temporal_split(df: pd.DataFrame, test_days: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Last `test_days` of grid time are the test set; everything earlier trains."""
    cutoff = df["t"].max() - pd.Timedelta(days=test_days)
    train = df[df["t"] <= cutoff].copy()
    test = df[df["t"] > cutoff].copy()
    return train, test


def add_occupancy_prior(
    train: pd.DataFrame, test: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(station, weekday, hour) mean occupancy + empty-rate, learned on TRAIN
    only, keyed by the TARGET slot. Doubles as the seasonal-naive baseline and is
    persisted in the model bundle so serving can reproduce these features."""
    prior = (
        train.dropna(subset=["occ"])
        .groupby(["station_id", "dow_tgt", "hour_tgt"])
        .agg(prior_occ=("occ", "mean"))
        .reset_index()
    )
    # empty rate by slot from the realized targets in train
    erate = (
        train.dropna(subset=["y_empty"])
        .groupby(["station_id", "dow_tgt", "hour_tgt"])
        .agg(prior_empty_rate=("y_empty", "mean"))
        .reset_index()
    )
    prior = prior.merge(erate, on=["station_id", "dow_tgt", "hour_tgt"], how="outer")
    keys = ["station_id", "dow_tgt", "hour_tgt"]
    return (train.merge(prior, on=keys, how="left"),
            test.merge(prior, on=keys, how="left"),
            prior)


FEATURES = [
    "bikes", "docks", "occ", "capacity", "staleness_min",
    "bikes_lag1", "bikes_lag2", "bikes_lag4", "flow_15", "flow_60",
    "roll_mean_1h", "roll_std_1h",
    "hour", "dow", "hour_tgt", "dow_tgt", "is_weekend",
    "hour_sin", "hour_cos", "hour_tgt_sin", "hour_tgt_cos",
    "prior_occ", "prior_empty_rate",
    "temperature", "precipitation", "wind_speed", "relative_humidity",
    "cloud_cover", "is_rain",
]


def _metrics_reg(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    err = y_pred - y_true
    return {"MAE": float(np.mean(np.abs(err))), "RMSE": float(np.sqrt(np.mean(err**2)))}


# ---------- model persistence & serving -----------------------------------------

def default_model_path(horizon_min: int) -> Path:
    return OUT / f"forecast_model_h{horizon_min}.joblib"


def save_bundle(path: Path, *, reg, clf, prior: pd.DataFrame,
                capacity: pd.DataFrame, config: dict) -> None:
    """Persist everything serving needs: both models, the feature list, the
    train-only (station, weekday, hour) prior, per-station effective capacity,
    and the config (grid/horizon/weather) so features rebuild identically."""
    import joblib
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"reg": reg, "clf": clf, "features": FEATURES,
         "prior": prior, "capacity": capacity, "config": config},
        path,
    )


def load_bundle(path: Path) -> dict:
    import joblib
    if not path.exists():
        sys.exit(f"model bundle not found: {path}\nTrain one first with --save-model.")
    return joblib.load(path)


def predict_live(data_dir: Path, horizons: list[int], top_n: int,
                 make_map: bool = True, lookback_hours: int = 48) -> int:
    """Serving: forecast every station at one or more horizons from the most
    recent data, using FORECAST weather for the target hours. Writes a
    LONG-format forecast_live.parquet — one row per (station, horizon).

    Only the latest slot per station is used, so the panel is bounded to the last
    `lookback_hours` — bounding memory/CPU regardless of how much history exists."""
    horizons = sorted(set(horizons))
    bundles = {h: load_bundle(default_model_path(h)) for h in horizons}  # fail early if missing
    base_cfg = bundles[horizons[0]]["config"]
    step_min, outage = base_cfg["step_min"], base_cfg["outage_cap_hours"]
    use_weather = any(b["config"]["use_weather"] for b in bundles.values())
    print(f"loaded {len(bundles)} bundle(s) for horizons {horizons} min")

    con = duckdb.connect()
    con.execute(
        f"CREATE OR REPLACE VIEW stations AS SELECT * FROM '{data_dir / 'stations.parquet'}'"
    )

    print(f"building panel from current data (last {lookback_hours}h)…")
    build_panel(con, data_dir, step_min, outage, lookback_hours=lookback_hours)
    now_ts = pd.Timestamp(con.execute("SELECT max(t) FROM panel").fetchone()[0])
    max_target = now_ts + pd.Timedelta(minutes=max(horizons))
    print(f"latest slot {now_ts}; forecasting up to {max_target}")

    wstatus = "off"
    if use_weather:
        # Forecast-weather swap: top up the store so all TARGET hours are covered.
        # Past hours come back as DWD observations; future ones as MOSMIX forecasts.
        try:
            fetch_weather(
                data_dir, HAMBURG_LAT, HAMBURG_LON,
                now_ts.floor("h").to_pydatetime(),
                max_target.ceil("h").to_pydatetime(),
                False, 7,
            )
        except Exception as e:  # noqa: BLE001 — serving must not die on a weather hiccup
            print(f"weather top-up skipped ({e})")
        build_weather_view(con, weather_path(data_dir))
        wmax, wfc = con.execute(
            "SELECT max(timestamp), COALESCE(sum(is_forecast::INT), 0) FROM weather"
        ).fetchone()
        wstatus = f"store through {wmax}, {int(wfc)} forecast hours"
    print(f"weather: {wstatus}")

    use_w = use_weather and weather_path(data_dir).exists()
    meta = con.execute("SELECT station_id, name, lat, lon FROM stations").df()

    frames = []
    for h in horizons:
        bundle = bundles[h]
        df = build_feature_frame(con, bundle["config"]["horizon_steps"], step_min, use_w)
        # Most recent slot per station with a known current level.
        live = (df.dropna(subset=["bikes"])
                  .sort_values("t").groupby("station_id", as_index=False).tail(1).copy())
        # Reattach the persisted prior (a training artifact — not recomputed at serving).
        live = live.merge(bundle["prior"], on=["station_id", "dow_tgt", "hour_tgt"], how="left")
        X = live[bundle["features"]]
        delta = bundle["reg"].predict(X)
        live["pred_bikes"] = np.clip(
            live["bikes"].to_numpy(float) + delta, 0, live["capacity"].to_numpy(float)
        )
        live["p_empty"] = bundle["clf"].predict_proba(X)[:, 1]
        live["horizon_min"] = h
        live["t_target"] = now_ts + pd.Timedelta(minutes=h)
        frames.append(
            live.merge(meta, on="station_id", how="left")
            [["station_id", "name", "lat", "lon", "t", "horizon_min", "t_target",
              "bikes", "capacity", "pred_bikes", "p_empty"]]
            .rename(columns={"bikes": "bikes_now"})
        )
        print(f"  +{h:>3} min: {len(live)} stations, mean P(empty)={live['p_empty'].mean():.3f}")

    result = pd.concat(frames, ignore_index=True)
    OUT.mkdir(parents=True, exist_ok=True)
    out_parq = OUT / "forecast_live.parquet"
    # Atomic write: a reader (the web container) must never see a half-written file.
    tmp = out_parq.with_suffix(".parquet.tmp")
    result.to_parquet(tmp, index=False)
    os.replace(tmp, out_parq)
    print(f"\nforecast: {result['station_id'].nunique()} stations × {len(horizons)} horizons "
          f"→ {out_parq.relative_to(REPO)}")

    # Primary horizon drives the standalone folium map + the md/console summary.
    primary = 60 if 60 in horizons else horizons[0]
    prim = (result[result["horizon_min"] == primary]
            .sort_values("p_empty", ascending=False).reset_index(drop=True))
    ptarget = pd.Timestamp(now_ts + pd.Timedelta(minutes=primary))

    md = [
        "# StadtRad Hamburg — live utilization forecast\n",
        f"_Horizons {', '.join(str(h) for h in horizons)} min. Latest slot {now_ts}. "
        f"Weather: {wstatus}._\n",
        f"## Top {top_n} stockout risks at +{primary} min\n",
        "| station | bikes now | pred bikes | P(empty) |",
        "|---|---|---|---|",
    ]
    for _, r in prim.head(top_n).iterrows():
        md.append(f"| {r['name']} | {r['bikes_now']:.0f} | {r['pred_bikes']:.1f} | {r['p_empty']:.2f} |")
    (OUT / "forecast_live.md").write_text("\n".join(md) + "\n")

    if make_map:
        map_html = SITE / "index.html"
        build_forecast_map(prim, map_html, horizon_min=primary, now_ts=now_ts, target_ts=ptarget)
        print(f"map  → {map_html.relative_to(REPO)}")

    print(f"\ntop {min(top_n, len(prim))} stockout risks at +{primary} min:")
    print(prim.drop(columns=["lat", "lon"]).head(top_n).to_string(index=False))
    return 0


def build_forecast_map(result: pd.DataFrame, out_html: Path, *,
                       horizon_min: int, now_ts: pd.Timestamp, target_ts: pd.Timestamp) -> None:
    """Standalone interactive Leaflet map (folium): one marker per station,
    coloured + sized by stockout risk. Self-contained HTML — host it anywhere."""
    import folium
    from branca.colormap import LinearColormap

    df = result.dropna(subset=["lat", "lon"]).copy()
    cmap = LinearColormap(
        ["#2ecc71", "#f1c40f", "#e74c3c"], vmin=0.0, vmax=1.0,
        caption=f"P(station empty) in {horizon_min} min",
    )
    gen_local = now_ts.tz_convert(LOCAL_TZ)
    tgt_local = target_ts.tz_convert(LOCAL_TZ)

    m = folium.Map(location=[53.55, 10.00], zoom_start=12, tiles="CartoDB positron")
    for _, r in df.iterrows():
        p = float(r["p_empty"])
        folium.CircleMarker(
            location=[float(r["lat"]), float(r["lon"])],
            radius=4 + 8 * p,                       # red, high-risk markers pop
            color=None, fill=True, fill_color=cmap(p), fill_opacity=0.85,
            tooltip=f"{r['name']} — P(empty) {p:.0%}",
            popup=folium.Popup(
                f"<b>{r['name']}</b><br>"
                f"bikes now: {r['bikes_now']:.0f} / cap {r['capacity']:.0f}<br>"
                f"predicted in {horizon_min} min: <b>{r['pred_bikes']:.1f}</b><br>"
                f"P(empty): <b>{p:.0%}</b>",
                max_width=260,
            ),
        ).add_to(m)
    cmap.add_to(m)

    title = (
        '<div style="position:fixed;top:10px;left:60px;z-index:9999;background:white;'
        'padding:8px 12px;border-radius:6px;font-family:sans-serif;font-size:13px;'
        'box-shadow:0 1px 4px rgba(0,0,0,.3)">'
        '<b>StadtRad Hamburg — stockout forecast</b><br>'
        f'target {tgt_local:%a %H:%M} (+{horizon_min} min) · '
        f'generated {gen_local:%Y-%m-%d %H:%M} {LOCAL_TZ}</div>'
    )
    m.get_root().html.add_child(folium.Element(title))
    out_html.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out_html))


# ---------- main ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import lightgbm as lgb
    from sklearn.metrics import average_precision_score, precision_recall_curve

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--step-min", type=int, default=15, help="Grid resolution (min). Default 15.")
    p.add_argument("--horizon-min", type=int, default=60, help="Forecast horizon (min). Default 60.")
    p.add_argument("--test-days", type=int, default=7, help="Trailing days held out for test.")
    p.add_argument("--outage-cap-hours", type=int, default=2, help="Max forward-fill age.")
    p.add_argument("--no-weather", action="store_true", help="Skip the weather join.")
    p.add_argument("--out", type=Path, default=OUT / "forecast_report.md")
    p.add_argument("--save-model", action="store_true",
                   help="Persist trained models + feature artifacts for serving.")
    p.add_argument("--predict", action="store_true",
                   help="Serving mode: load saved model(s) and forecast the next horizon(s).")
    p.add_argument("--horizons", type=int, nargs="+", default=None,
                   help="Predict at these horizons in minutes (default: just --horizon-min). "
                        "Each needs a bundle trained with --save-model --horizon-min H.")
    p.add_argument("--top-n", type=int, default=20, help="Rows in the --predict risk table.")
    p.add_argument("--no-map", action="store_true", help="Skip the HTML map in --predict.")
    p.add_argument("--lookback-hours", type=int, default=48,
                   help="--predict: bound the panel to the last N hours (default 48). "
                        "Keeps serving memory/CPU flat as history grows.")
    args = p.parse_args(argv)

    if args.horizon_min % args.step_min:
        sys.exit(f"--horizon-min ({args.horizon_min}) must be a multiple of --step-min ({args.step_min})")
    horizon_steps = args.horizon_min // args.step_min

    data_dir = data_root()
    if args.predict:
        horizons = args.horizons or [args.horizon_min]
        return predict_live(data_dir, horizons, args.top_n,
                            make_map=not args.no_map, lookback_hours=args.lookback_hours)

    use_weather = not args.no_weather and weather_path(data_dir).exists()
    if not args.no_weather and not use_weather:
        print("note: no weather store found — run analysis/weather.py first. Continuing without it.")

    con = duckdb.connect()
    if use_weather:
        build_weather_view(con, weather_path(data_dir))

    print(f"building panel (step={args.step_min} min, outage cap={args.outage_cap_hours} h)…")
    build_panel(con, data_dir, args.step_min, args.outage_cap_hours)
    print(f"engineering features (horizon={args.horizon_min} min = {horizon_steps} steps, "
          f"weather={'on' if use_weather else 'off'})…")
    df = build_feature_frame(con, horizon_steps, args.step_min, use_weather)

    # Need current level + a defined target. Lag NaNs are fine (LightGBM handles them).
    df = df.dropna(subset=["bikes", "y_bikes"])
    print(f"rows: {len(df):,} | stations: {df['station_id'].nunique()} | "
          f"target empty-rate: {df['y_empty'].mean()*100:.1f}%  full-rate: {df['y_full'].mean()*100:.1f}%")

    train, test = temporal_split(df, args.test_days)
    train, test, prior = add_occupancy_prior(train, test)
    print(f"split → train {len(train):,} rows (≤ {train['t'].max()}), "
          f"test {len(test):,} rows (> {train['t'].max()})")

    Xtr, Xte = train[FEATURES], test[FEATURES]

    # ---- regression: bikes available at t+h ----
    # Delta target: predict the CHANGE in bikes over the horizon, then add it
    # back to the current level. A zero prediction reproduces persistence, so
    # the model is anchored to that strong baseline and can only refine it,
    # instead of regressing toward slot means and underperforming it.
    # L1 objective matches the MAE we report (the L2 default chases RMSE).
    reg = lgb.LGBMRegressor(
        objective="regression_l1",
        n_estimators=600, learning_rate=0.05, num_leaves=63,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=100,
        n_jobs=-1, verbosity=-1,
    )
    reg.fit(Xtr, train["y_bikes"] - train["bikes"])    # Δ = bikes(t+h) − bikes(t)
    pred = np.clip(test["bikes"].to_numpy(dtype=float) + reg.predict(Xte), 0, None)

    base_persist = test["bikes"].to_numpy(dtype=float)                       # value now
    base_season = (test["prior_occ"] * test["capacity"]).to_numpy(dtype=float)
    base_season = np.nan_to_num(base_season, nan=float(train["y_bikes"].mean()))
    yte = test["y_bikes"].to_numpy(dtype=float)

    reg_rows = [
        ("persistence (bikes now)", _metrics_reg(yte, base_persist)),
        ("seasonal-naive (slot mean)", _metrics_reg(yte, base_season)),
        ("LightGBM (Δ-target)", _metrics_reg(yte, pred)),
    ]

    # ---- classification: empty at t+h ----
    clf = lgb.LGBMClassifier(
        n_estimators=600, learning_rate=0.05, num_leaves=63,
        subsample=0.8, colsample_bytree=0.8, min_child_samples=100,
        n_jobs=-1, verbosity=-1,
    )
    clf.fit(Xtr, train["y_empty"])
    proba = clf.predict_proba(Xte)[:, 1]
    yemp = test["y_empty"].to_numpy()
    prior_score = np.nan_to_num(test["prior_empty_rate"].to_numpy(dtype=float))

    ap_model = average_precision_score(yemp, proba)
    ap_prior = average_precision_score(yemp, prior_score)
    prec, rec, thr = precision_recall_curve(yemp, proba)
    # recall achievable while holding precision ≥ 0.5 (a usable dispatch trigger)
    ok = prec[:-1] >= 0.5
    recall_at_p50 = float(rec[:-1][ok].max()) if ok.any() else 0.0

    # ---- feature importance (top 12) ----
    imp = (pd.Series(reg.feature_importances_, index=FEATURES)
           .sort_values(ascending=False).head(12))

    # ---- report ----
    lines: list[str] = []
    def out(s: str = "") -> None:
        print(s); lines.append(s)

    out("# StadtRad Hamburg — utilization forecast\n")
    out(f"_Horizon **{args.horizon_min} min**, grid {args.step_min} min, "
        f"weather {'on' if use_weather else 'off'}. Test = last {args.test_days} days._\n")
    out(f"- Rows {len(df):,} · stations {df['station_id'].nunique()} · "
        f"train {len(train):,} / test {len(test):,}")
    out(f"- Test target rates — empty {yemp.mean()*100:.1f}%, "
        f"full {test['y_full'].mean()*100:.1f}%\n")

    out("## Regression — bikes available at t+h\n")
    out("| model | MAE (bikes) | RMSE |")
    out("|---|---|---|")
    for name, m in reg_rows:
        out(f"| {name} | {m['MAE']:.3f} | {m['RMSE']:.3f} |")
    best_base = min(reg_rows[0][1]["MAE"], reg_rows[1][1]["MAE"])
    lift = (best_base - reg_rows[2][1]["MAE"]) / best_base * 100
    if lift >= 0:
        out(f"\nLightGBM reduces MAE **{lift:.1f}%** vs the best baseline "
            f"(persistence — a hard bar at this horizon).\n")
    else:
        out(f"\nLightGBM does **not** beat persistence on MAE here ({-lift:.1f}% worse); "
            "persistence is brutal at short horizons. The value shows up in the "
            "stockout classifier below and grows with horizon.\n")

    out("## Classification — station empty at t+h (the dispatch signal)\n")
    out(f"- PR-AUC: **LightGBM {ap_model:.3f}** vs seasonal prior {ap_prior:.3f} "
        f"(base rate {yemp.mean():.3f})")
    out(f"- Recall at precision ≥ 0.50: **{recall_at_p50*100:.1f}%** "
        "(share of upcoming stockouts caught while ≥ half the alerts are real)\n")

    out("## Top features (regression gain)\n")
    for name, v in imp.items():
        out(f"- {name}: {int(v)}")

    out_path = args.out if args.out.is_absolute() else (REPO / args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n")
    try:
        shown = out_path.relative_to(REPO)
    except ValueError:
        shown = out_path
    print(f"\nreport → {shown}")

    if args.save_model:
        config = {
            "step_min": args.step_min,
            "horizon_min": args.horizon_min,
            "horizon_steps": horizon_steps,
            "outage_cap_hours": args.outage_cap_hours,
            "use_weather": use_weather,
            "local_tz": LOCAL_TZ,
            "cap_quantile": CAP_QUANTILE,
            "trained_through": str(train["t"].max()),
        }
        cap_df = con.execute("SELECT * FROM cap").df()
        model_path = default_model_path(args.horizon_min)
        save_bundle(model_path, reg=reg, clf=clf, prior=prior, capacity=cap_df, config=config)
        print(f"saved model bundle → {model_path.relative_to(REPO)} "
              f"(serve with: python analysis/forecast.py --predict --horizons {args.horizon_min})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

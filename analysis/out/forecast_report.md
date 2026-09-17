# StadtRad Hamburg — utilization forecast

_Horizon **120 min**, grid 15 min, weather on. Test = last 7 days._

- Rows 1,181,338 · stations 358 · train 942,215 / test 239,123
- Test target rates — empty 10.6%, full 2.0%

## Regression — bikes available at t+h

| model | MAE (bikes) | RMSE |
|---|---|---|
| persistence (bikes now) | 1.298 | 2.665 |
| seasonal-naive (slot mean) | 5.516 | 7.960 |
| LightGBM (Δ-target) | 1.266 | 2.407 |

LightGBM reduces MAE **2.5%** vs the best baseline (persistence — a hard bar at this horizon).

## Classification — station empty at t+h (the dispatch signal)

- PR-AUC: **LightGBM 0.491** vs seasonal prior 0.251 (base rate 0.106)
- Recall at precision ≥ 0.50: **50.4%** (share of upcoming stockouts caught while ≥ half the alerts are real)

## Top features (regression gain)

- prior_occ: 6190
- prior_empty_rate: 4022
- hour: 3197
- occ: 2207
- roll_std_1h: 1983
- temperature: 1965
- dow: 1902
- capacity: 1763
- roll_mean_1h: 1664
- relative_humidity: 1627
- bikes: 1530
- flow_60: 1125

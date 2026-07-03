# StadtRad Hamburg — utilization forecast

_Horizon **120 min**, grid 15 min, weather on. Test = last 7 days._

- Rows 1,181,338 · stations 358 · train 942,215 / test 239,123
- Test target rates — empty 10.6%, full 2.0%

## Regression — bikes available at t+h

| model | MAE (bikes) | RMSE |
|---|---|---|
| persistence (bikes now) | 1.298 | 2.665 |
| seasonal-naive (slot mean) | 5.516 | 7.960 |
| LightGBM (Δ-target) | 1.265 | 2.409 |

LightGBM reduces MAE **2.5%** vs the best baseline (persistence — a hard bar at this horizon).

## Classification — station empty at t+h (the dispatch signal)

- PR-AUC: **LightGBM 0.491** vs seasonal prior 0.251 (base rate 0.106)
- Recall at precision ≥ 0.50: **50.2%** (share of upcoming stockouts caught while ≥ half the alerts are real)

## Top features (regression gain)

- prior_occ: 5894
- prior_empty_rate: 4310
- hour: 3001
- occ: 2409
- roll_std_1h: 2270
- capacity: 2047
- temperature: 1919
- roll_mean_1h: 1749
- relative_humidity: 1660
- dow: 1615
- bikes: 1512
- hour_tgt: 1440

# StadtRad Hamburg — utilization forecast

_Horizon **120 min**, grid 15 min, weather on. Test = last 7 days._

- Rows 945,055 · stations 356 · train 706,495 / test 238,560
- Test target rates — empty 11.4%, full 2.1%

## Regression — bikes available at t+h

| model | MAE (bikes) | RMSE |
|---|---|---|
| persistence (bikes now) | 1.458 | 2.953 |
| seasonal-naive (slot mean) | 5.611 | 7.935 |
| LightGBM (Δ-target) | 1.432 | 2.707 |

LightGBM reduces MAE **1.8%** vs the best baseline (persistence — a hard bar at this horizon).

## Classification — station empty at t+h (the dispatch signal)

- PR-AUC: **LightGBM 0.503** vs seasonal prior 0.297 (base rate 0.114)
- Recall at precision ≥ 0.50: **49.6%** (share of upcoming stockouts caught while ≥ half the alerts are real)

## Top features (regression gain)

- prior_occ: 5722
- prior_empty_rate: 4209
- hour: 2879
- roll_std_1h: 2125
- roll_mean_1h: 2110
- temperature: 2094
- capacity: 2005
- occ: 1998
- dow: 1635
- relative_humidity: 1525
- bikes: 1457
- wind_speed: 1352

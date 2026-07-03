# StadtRad Hamburg — utilization forecast

_Horizon **60 min**, grid 15 min, weather on. Test = last 7 days._

- Rows 946,479 · stations 356 · train 707,919 / test 238,560
- Test target rates — empty 11.4%, full 2.1%

## Regression — bikes available at t+h

| model | MAE (bikes) | RMSE |
|---|---|---|
| persistence (bikes now) | 0.915 | 1.938 |
| seasonal-naive (slot mean) | 5.519 | 7.805 |
| LightGBM (Δ-target) | 0.946 | 1.853 |

LightGBM does **not** beat persistence on MAE here (3.4% worse); persistence is brutal at short horizons. The value shows up in the stockout classifier below and grows with horizon.

## Classification — station empty at t+h (the dispatch signal)

- PR-AUC: **LightGBM 0.548** vs seasonal prior 0.297 (base rate 0.114)
- Recall at precision ≥ 0.50: **56.3%** (share of upcoming stockouts caught while ≥ half the alerts are real)

## Top features (regression gain)

- prior_occ: 5987
- prior_empty_rate: 5761
- roll_std_1h: 2254
- roll_mean_1h: 2252
- hour: 2159
- occ: 1930
- flow_60: 1796
- capacity: 1292
- temperature: 1243
- dow: 1183
- hour_tgt_cos: 1167
- relative_humidity: 1163

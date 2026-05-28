# Stadtrad Hamburg: Open Data Science
Data Science with open data from the Stadtrad Hamburg Portal.

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

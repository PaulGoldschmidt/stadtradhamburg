"""Public StadtRad Hamburg website — FastAPI app over the DuckDB/Parquet store.

Read-only. Serves a single-page dashboard plus a curated JSON API. The heavy
trip reconstruction is cached (rebuilt on a background timer), everything else is
TTL-cached, and access is rate-limited — see the module docstrings and
`deploy/web/` for the container + resource limits.
"""

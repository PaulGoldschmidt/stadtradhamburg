"""Smoke test + latency/memory budget for the web app, against the local
store and derived store. Starts uvicorn on a spare port, hits every endpoint
twice (cold cache, then warm), prints status, latency and response size, the
process RSS, then stops the server.

    .venv/bin/python scripts/smoke_web.py            # uses data/ + derived/
    DERIVED_DIR=/elsewhere .venv/bin/python scripts/smoke_web.py

The numbers are the budget the deployment accepts: the web is tuned for a
minimal memory footprint, so a cold history endpoint may take a few hundred
milliseconds. Run scripts/derive.py first so derived/ exists.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("SMOKE_PORT", "8765"))
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str) -> tuple[int, float, int, bytes]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=60) as r:
            body = r.read()
            return r.status, time.perf_counter() - t0, len(body), body
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t0, 0, b""


def rss_mb(pid: int) -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024


def main() -> int:
    env = dict(os.environ)
    env.setdefault("DUCKDB_TEMP_DIR", "/tmp/stadtrad-duckdb-smoke")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.app:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=REPO, env=env,
    )
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                if get("/api/health")[0] == 200:
                    break
            except Exception:
                pass
        else:
            print("server did not come up")
            return 1
        print(f"startup RSS: {rss_mb(proc.pid):.0f} MB")

        status, _, _, body = get("/api/stations")
        stations = json.loads(body) if status == 200 else []
        sid = stations[0]["station_id"] if stations else "unknown"
        endpoints = [
            "/api/health", "/api/stats/today", "/api/stations",
            "/api/history/summary", "/api/history/trips_daily", "/api/history/heatmap",
            "/api/history/top_stations", "/api/history/bikes", "/api/weather/ridership",
            "/api/forecast/horizons", "/api/forecast?horizon=60",
            f"/api/station/{sid}", f"/api/station/{sid}/insights",
            "/api/download/trips_daily.csv", "/api/download/stations.parquet",
            "/", "/forecast", f"/station/{sid}",
        ]
        ok = True
        for label in ("cold", "warm"):
            print(f"--- {label}")
            for ep in endpoints:
                status, dt, size, _ = get(ep)
                flag = "" if status == 200 else "  <-- FAIL"
                ok &= status == 200
                print(f"{status} {dt*1000:7.1f} ms {size:9d} B  {ep}{flag}")
        print(f"RSS after serving everything twice: {rss_mb(proc.pid):.0f} MB")
        summary = json.loads(get("/api/history/summary")[3] or b"{}")
        print("history summary:", summary)
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())

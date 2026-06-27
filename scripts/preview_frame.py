"""One-shot frame preview to check the renderer's look without encoding video.

Usage:
    .venv/bin/python scripts/preview_frame.py --day 2026-06-23 --at-hour 17.5

Renders a single matplotlib frame at the requested time and saves a PNG.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pytz

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "analysis"))

from _trips import LOCAL_TZ, build_trips, build_views, data_root  # noqa: E402
from clip.render import (  # noqa: E402
    HAMBURG_BBOX, HEAD_RGB, TAIL_RGB,
    _prep_tracks, _seg_colors,
)
from clip.routes import load_cache  # noqa: E402

import contextily as cx  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", required=True, help="YYYY-MM-DD local")
    p.add_argument("--at-hour", type=float, default=17.5,
                   help="Time of day to sample (float hours, local).")
    p.add_argument("--trail-seconds", type=float, default=1800.0)
    p.add_argument("--simplify", type=float, default=1e-4)
    p.add_argument("--linewidth", type=float, default=2.4)
    p.add_argument("--figsize", type=float, nargs=2, default=(16, 9))
    p.add_argument("--dpi", type=int, default=140)
    p.add_argument("--out", type=Path, default=REPO / "analysis" / "out" / "preview_frame.png")
    args = p.parse_args()

    tz = pytz.timezone(LOCAL_TZ)
    day = dt.datetime.strptime(args.day, "%Y-%m-%d").date()
    h = int(args.at_hour); m = int((args.at_hour - h) * 60)
    sample_t = tz.localize(dt.datetime.combine(day, dt.time(h, m)))
    window_start = tz.localize(dt.datetime.combine(day, dt.time(0, 0)))
    window_end = tz.localize(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0)))

    con = duckdb.connect()
    build_views(con, data_root())
    build_trips(con)
    print("loading trips for the day…")
    trips = con.execute(
        """
        SELECT bike_number, origin_id, destination_id,
               departed_after, arrived_by,
               origin_lat, origin_lon, dest_lat, dest_lon
        FROM trips_geo
        WHERE departed_after >= ?::TIMESTAMPTZ
          AND departed_after <  ?::TIMESTAMPTZ
        """,
        [window_start, window_end],
    ).df()
    for col in ("departed_after", "arrived_by"):
        if trips[col].dt.tz is None:
            trips[col] = trips[col].dt.tz_localize("UTC")
    print(f"{len(trips):,} trips")

    routes = load_cache(REPO / "analysis" / "out")
    print(f"{len(routes):,} routes cached")

    segs, seg_trip_idx, seg_within, trip_start, trip_end = _prep_tracks(
        trips, routes, simplify_eps=args.simplify
    )
    n_trips = len(trip_start)
    trip_nseg = np.zeros(n_trips, dtype=np.int32)
    np.add.at(trip_nseg, seg_trip_idx, 1)

    pos = seg_within.astype(np.float32) / np.maximum(
        trip_nseg[seg_trip_idx].astype(np.float32) - 1.0, 1.0
    )
    rgb_pos = (TAIL_RGB[None, :] + (HEAD_RGB - TAIL_RGB)[None, :] * pos[:, None]).astype(np.float32)
    alpha_pos = (0.12 + 0.78 * pos).astype(np.float32)

    colors = _seg_colors(
        sample_t.timestamp(), trip_start, trip_end, seg_trip_idx, seg_within,
        trip_nseg, args.trail_seconds, rgb_pos=rgb_pos, alpha_pos=alpha_pos,
    )

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=tuple(args.figsize), facecolor="black", dpi=args.dpi)
    ax.set_facecolor("black")
    ax.set_xlim(HAMBURG_BBOX[0], HAMBURG_BBOX[2])
    ax.set_ylim(HAMBURG_BBOX[1], HAMBURG_BBOX[3])
    mid_lat = (HAMBURG_BBOX[1] + HAMBURG_BBOX[3]) / 2
    ax.set_aspect(1.0 / np.cos(np.radians(mid_lat)))
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    print("drawing basemap (composite: base @ zoom 13 + labels @ zoom 10)…")
    cx.add_basemap(
        ax, crs="EPSG:4326",
        source=cx.providers.CartoDB.DarkMatterNoLabels(r="@2x"),
        zoom=13, attribution=False, interpolation="bilinear",
    )
    ax.set_xlim(HAMBURG_BBOX[0], HAMBURG_BBOX[2])
    ax.set_ylim(HAMBURG_BBOX[1], HAMBURG_BBOX[3])
    cx.add_basemap(
        ax, crs="EPSG:4326",
        source=cx.providers.CartoDB.DarkMatterOnlyLabels(r="@2x"),
        zoom=10, attribution=False, interpolation="bilinear",
    )
    ax.set_xlim(HAMBURG_BBOX[0], HAMBURG_BBOX[2])
    ax.set_ylim(HAMBURG_BBOX[1], HAMBURG_BBOX[3])

    lc = LineCollection(segs, linewidths=args.linewidth, capstyle="round")
    lc.set_colors(colors)
    ax.add_collection(lc)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(args.out), dpi=args.dpi, facecolor="black", bbox_inches=None, pad_inches=0)
    plt.close(fig)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

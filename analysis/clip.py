"""Render a dark-theme animated MP4 of StadtRad trips for a chosen window.

Pipeline:
  1. Reconstruct trips via the shared `_trips.build_trips` helper.
  2. Look up (or fetch via OSRM) a routed polyline per unique OD pair.
  3. Render frames with matplotlib + ffmpeg → MP4.

Examples:
  python analysis/clip.py --day 2026-06-23
  python analysis/clip.py --day 2026-06-23 --start-hour 17 --end-hour 18 --time-scale 60
  python analysis/clip.py --start 2026-05-29 --end 2026-06-26 --time-scale 3600
  python analysis/clip.py --day 2026-06-23 --routes-only

Requires:
  - OSRM running locally — see deploy/osrm/README.md
  - ffmpeg on PATH (brew install ffmpeg)
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytz

# Stay importable both as `python analysis/clip.py` and `python -m analysis.clip`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _trips import LOCAL_TZ, build_trips, build_views, data_root  # noqa: E402

from clip.osrm import OSRM_DEFAULT  # noqa: E402
from clip.routes import ensure_routes, load_cache  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "analysis" / "out"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--day", help="One local date, YYYY-MM-DD (Europe/Berlin).")
    when.add_argument("--start", help="Window start, YYYY-MM-DD (local).")
    p.add_argument("--end", help="Window end (exclusive), YYYY-MM-DD (local).")
    p.add_argument("--start-hour", type=int, default=0, help="Trim hours from window start (local).")
    p.add_argument("--end-hour", type=int, default=24, help="Trim hours to window end (local).")

    p.add_argument("--time-scale", type=float, default=300.0,
                   help="Real seconds per video second. Default 300 (1 s = 5 min).")
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--trail-seconds", type=float, default=1800.0,
                   help="How long a finished trip's trail lingers (real seconds).")
    p.add_argument("--linewidth", type=float, default=None,
                   help="Stroke width in points; auto-scales to resolution if unset.")
    p.add_argument("--resolution", choices=["1080p", "1440p", "4k"], default="4k",
                   help="Output resolution preset. 4k = 3840×2160. Lower presets render faster.")
    p.add_argument("--figsize", type=float, nargs=2, default=None,
                   metavar=("W", "H"),
                   help="Override figure size (inches). Overrides --resolution.")
    p.add_argument("--dpi", type=int, default=None,
                   help="Override DPI. Overrides --resolution.")
    p.add_argument("--bitrate", type=int, default=12000,
                   help="Encoder bitrate (kbps). Default 12000 (good for 4K).")
    p.add_argument("--basemap-zoom", default="auto",
                   help="contextily basemap zoom; 'auto' or an integer.")
    p.add_argument("--label-zoom", type=int, default=None,
                   help="If set, draw a composite basemap: NoLabels at base "
                        "zoom + OnlyLabels at this (typically lower) zoom for "
                        "bigger city names. Off by default.")
    p.add_argument("--bbox", type=float, nargs=4,
                   metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                   help="Override map bounding box.")
    p.add_argument("--simplify-routes", type=float, default=1e-4,
                   help="RDP tolerance in degrees (~10 m at Hamburg). 0 to disable.")

    p.add_argument("--osrm", default=OSRM_DEFAULT, help="OSRM base URL.")
    p.add_argument("--routes-only", action="store_true",
                   help="Build/refresh the route cache, then exit.")
    p.add_argument("--no-render", action="store_true",
                   help="Same as --routes-only but kept for backward parity.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output MP4 path (default analysis/out/clip_<window>.mp4).")
    return p.parse_args(argv)


def resolve_window(args: argparse.Namespace) -> tuple[dt.datetime, dt.datetime, str]:
    tz = pytz.timezone(LOCAL_TZ)
    if args.day:
        day = dt.datetime.strptime(args.day, "%Y-%m-%d").date()
        start = tz.localize(dt.datetime.combine(day, dt.time(args.start_hour, 0)))
        end_hour = args.end_hour
        if end_hour >= 24:
            end = tz.localize(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0)))
        else:
            end = tz.localize(dt.datetime.combine(day, dt.time(end_hour, 0)))
        label = args.day
    else:
        if not args.end:
            sys.exit("--start requires --end")
        s = dt.datetime.strptime(args.start, "%Y-%m-%d").date()
        e = dt.datetime.strptime(args.end, "%Y-%m-%d").date()
        start = tz.localize(dt.datetime.combine(s, dt.time(args.start_hour, 0)))
        end_hour = args.end_hour
        if end_hour >= 24:
            end = tz.localize(dt.datetime.combine(e + dt.timedelta(days=1), dt.time(0, 0)))
        else:
            end = tz.localize(dt.datetime.combine(e, dt.time(end_hour, 0)))
        label = f"{args.start}_{args.end}"
    return start, end, label


def load_trips_in_window(
    con: duckdb.DuckDBPyConnection, start: dt.datetime, end: dt.datetime
) -> pd.DataFrame:
    """Pull trips whose start_time falls in the window. Returns a tz-aware
    pandas DataFrame in the columns the renderer expects."""
    df = con.execute(
        """
        SELECT bike_number,
               origin_id, destination_id,
               departed_after, arrived_by,
               origin_lat, origin_lon,
               dest_lat,   dest_lon
        FROM trips_geo
        WHERE departed_after >= ?::TIMESTAMPTZ
          AND departed_after <  ?::TIMESTAMPTZ
        """,
        [start, end],
    ).df()
    if df.empty:
        return df
    for col in ("departed_after", "arrived_by"):
        if df[col].dt.tz is None:
            df[col] = df[col].dt.tz_localize("UTC")
    return df


def default_out_path(label: str) -> Path:
    return OUT / f"clip_{label}.mp4"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    window_start, window_end, label = resolve_window(args)
    print(f"window: {window_start} → {window_end} (label={label})")

    # Build trips once for the whole dataset; we filter per-window when reading.
    print("loading bikes/stations + reconstructing trips…")
    con = duckdb.connect()
    build_views(con, data_root())
    build_trips(con)

    OUT.mkdir(parents=True, exist_ok=True)

    # Routes phase: cache covers every OD pair across the dataset, so a single
    # build pays off all future windows.
    print("ensuring route cache…")
    routes = ensure_routes(con, OUT, base_url=args.osrm)

    if args.routes_only or args.no_render:
        print(f"routes-only mode: {len(routes):,} cached pairs. Done.")
        return 0

    trips = load_trips_in_window(con, window_start, window_end)
    print(f"window contains {len(trips):,} trips")
    if trips.empty:
        sys.exit("no trips in window — nothing to render")

    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ffmpeg not found on PATH. Install with `brew install ffmpeg` "
            "(macOS) or your distro's package manager."
        )

    # Import the renderer late so missing optional deps only crash when needed.
    from clip.render import HAMBURG_BBOX, render

    bbox = tuple(args.bbox) if args.bbox else HAMBURG_BBOX
    out_path = args.out or default_out_path(label)

    basemap_zoom = args.basemap_zoom
    if basemap_zoom != "auto":
        basemap_zoom = int(basemap_zoom)

    # Resolution presets: pixel = figsize_inch * dpi. 16:9 widescreen.
    # The 4K preset uses plain DarkMatter @2x (labels render readable thanks
    # to the high pixel density). Lower-res presets enable the composite so
    # city names stay legible.
    # Each tuple: (figsize, dpi, linewidth, basemap_zoom, default_label_zoom)
    presets = {
        "1080p": ((16, 9), 120, 1.4, 13, 10),
        "1440p": ((16, 9), 160, 1.8, 13, 10),
        "4k":    ((16, 9), 240, 2.4, 12, None),
    }
    preset_figsize, preset_dpi, preset_lw, preset_zoom, preset_label_zoom = (
        presets[args.resolution]
    )
    figsize = tuple(args.figsize) if args.figsize else preset_figsize
    dpi = args.dpi or preset_dpi
    linewidth = args.linewidth if args.linewidth is not None else preset_lw
    if args.basemap_zoom == "auto":
        basemap_zoom = preset_zoom

    label_zoom = args.label_zoom if args.label_zoom is not None else preset_label_zoom

    render(
        trips=trips,
        routes=routes,
        window_start=window_start,
        window_end=window_end,
        out_path=out_path,
        time_scale=args.time_scale,
        fps=args.fps,
        trail_seconds=args.trail_seconds,
        bbox=bbox,
        figsize=figsize,
        bitrate=args.bitrate,
        dpi=dpi,
        linewidth=linewidth,
        basemap_zoom=basemap_zoom,
        label_zoom=label_zoom,
        simplify_eps=args.simplify_routes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

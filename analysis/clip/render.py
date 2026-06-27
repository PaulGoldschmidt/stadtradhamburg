"""Dark-theme animated trip clip — fading routed traces over a Hamburg basemap.

Visualization design:
  - All trips pre-flattened into one big segment array. Per frame, a NumPy
    mask picks visible segments and computes per-segment color+alpha. The
    matplotlib LineCollection's segment list and color list are swapped in
    place — blit-friendly, ~10-30 ms per frame on a M-series Mac.
  - Color: warm gradient. Tail of an active trip → magenta-ish; head → bright
    yellow. Trail (after arrival) fades all segments to transparent over
    `trail_seconds`.
  - Basemap: CartoDB Dark Matter via contextily, drawn once into the axes.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Mapping

import contextily as cx
import matplotlib.animation as animation
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from tqdm import tqdm

# Hamburg bbox (lon_min, lat_min, lon_max, lat_max). Tuned to the StadtRad
# service area (Wedel/Rissen → Bergedorf, Norderstedt → Harburg) widened
# east/west to a ~16:9 aspect once the latitude-cos correction is applied.
HAMBURG_BBOX = (9.50, 53.40, 10.55, 53.75)

# Cool head→tail gradient. position_frac=1 → head (icy near-white);
# 0 → tail (deep azure). Combined with the alpha ramp this gives bikes a
# bright-comet head fading into a darker body.
HEAD_RGB = np.array([0.88, 0.97, 1.00])  # #E0F8FF near-white cyan
TAIL_RGB = np.array([0.12, 0.25, 0.69])  # #1E40AF deep azure


def _rdp(points: np.ndarray, eps: float) -> np.ndarray:
    """Ramer–Douglas–Peucker polyline simplification, iterative.

    `points` shape (N, 2). Returns a subset that approximates the polyline to
    `eps` (same units as the coordinates — for lat/lon, 1e-4 ≈ ~10 m).
    Vectorized perpendicular-distance per recursion, so plenty fast for the
    ~50-200-point routes OSRM gives us."""
    n = len(points)
    if n < 3 or eps <= 0:
        return points
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = points[i], points[j]
        d = b - a
        norm = float(np.hypot(d[0], d[1]))
        if norm == 0.0:
            continue
        mid = points[i + 1 : j]
        # 2D cross product magnitude / segment length = perpendicular distance.
        diff = mid - a
        cross = diff[:, 0] * d[1] - diff[:, 1] * d[0]
        dists = np.abs(cross) / norm
        k = int(np.argmax(dists))
        if dists[k] > eps:
            split = i + 1 + k
            keep[split] = True
            stack.append((i, split))
            stack.append((split, j))
    return points[keep]


def _prep_tracks(
    trips: pd.DataFrame,
    routes: Mapping[tuple[str, str], dict],
    simplify_eps: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Flatten per-trip polylines into a global segment array.

    Returns:
      all_segments  (M, 2, 2) — (lon, lat) endpoints per segment
      seg_trip_idx  (M,)     — which trip each segment belongs to
      seg_within    (M,)     — segment index within its trip (0..n_seg-1)
      trip_start    (T,)     — start time as seconds-since-epoch
      trip_end      (T,)     — end time as seconds-since-epoch
    """
    segs_per_trip: list[np.ndarray] = []
    trip_idx_per_seg: list[np.ndarray] = []
    within_per_seg: list[np.ndarray] = []
    start_ts: list[float] = []
    end_ts: list[float] = []
    raw_points = 0
    kept_points = 0

    for ti, t in enumerate(
        tqdm(trips.itertuples(index=False), total=len(trips), desc="prep tracks", unit="trip")
    ):
        key = (t.origin_id, t.destination_id)
        r = routes.get(key)
        geom = (r or {}).get("geometry")
        if not geom:
            # Fallback to straight line.
            geom = [[float(t.origin_lon), float(t.origin_lat)],
                    [float(t.dest_lon),   float(t.dest_lat)]]
        coords = np.asarray(geom, dtype=np.float32)  # (N, 2)
        raw_points += len(coords)
        if simplify_eps > 0.0 and len(coords) > 2:
            coords = _rdp(coords, simplify_eps).astype(np.float32, copy=False)
        kept_points += len(coords)
        if coords.shape[0] < 2:
            continue
        # Segments: each row is [[lon_i, lat_i], [lon_{i+1}, lat_{i+1}]]
        n = coords.shape[0] - 1
        segs = np.stack([coords[:-1], coords[1:]], axis=1)  # (n, 2, 2)
        segs_per_trip.append(segs)
        trip_idx_per_seg.append(np.full(n, ti, dtype=np.int32))
        within_per_seg.append(np.arange(n, dtype=np.int32))
        start_ts.append(t.departed_after.timestamp())
        end_ts.append(t.arrived_by.timestamp())

    all_segments = np.concatenate(segs_per_trip, axis=0)
    seg_trip_idx = np.concatenate(trip_idx_per_seg)
    seg_within = np.concatenate(within_per_seg)
    trip_start = np.asarray(start_ts, dtype=np.float64)
    trip_end = np.asarray(end_ts, dtype=np.float64)
    if simplify_eps > 0.0 and raw_points:
        print(
            f"render: RDP eps={simplify_eps} kept {kept_points:,}/{raw_points:,} "
            f"points ({kept_points / raw_points * 100:.0f}%)"
        )
    return all_segments, seg_trip_idx, seg_within, trip_start, trip_end


def _seg_colors(
    t: float,
    trip_start: np.ndarray,
    trip_end: np.ndarray,
    seg_trip_idx: np.ndarray,
    seg_within: np.ndarray,
    trip_nseg: np.ndarray,
    trail_seconds: float,
    *,
    rgb_pos: np.ndarray,    # precomputed per-segment color ramp
    alpha_pos: np.ndarray,  # precomputed per-segment alpha base
) -> np.ndarray:
    """All-segment RGBA. Invisible segments get alpha=0 — matplotlib renders
    those for free, and avoiding per-frame index/slice churn keeps the
    LineCollection's internal path arrays the same shape every frame (its
    fast path)."""
    ts = trip_start[seg_trip_idx]
    te = trip_end[seg_trip_idx]
    ns = trip_nseg[seg_trip_idx].astype(np.float32)

    duration = np.maximum(te - ts, 1.0)
    progress = np.clip((t - ts) / duration, 0.0, 1.0)
    seg_thresh = (seg_within.astype(np.float32) + 1.0) / np.maximum(ns, 1.0)

    in_active = (ts <= t) & (t <= te)
    in_trail = (te < t) & (t <= te + trail_seconds)
    visible_active = in_active & (progress >= seg_thresh)

    trail_age = np.where(in_trail, (t - te) / max(trail_seconds, 1.0), 0.0)
    fade = np.where(in_trail, 1.0 - trail_age, 1.0).astype(np.float32)
    fade = np.clip(fade, 0.0, 1.0)

    visible = visible_active | in_trail
    alpha = np.where(visible, alpha_pos * fade, 0.0).astype(np.float32)
    return np.concatenate([rgb_pos, alpha[:, None]], axis=1)


def render(
    trips: pd.DataFrame,
    routes: Mapping[tuple[str, str], dict],
    window_start: dt.datetime,
    window_end: dt.datetime,
    out_path: Path,
    *,
    time_scale: float = 300.0,
    fps: int = 30,
    trail_seconds: float = 1800.0,
    bbox: tuple[float, float, float, float] = HAMBURG_BBOX,
    figsize: tuple[float, float] = (10, 10),
    bitrate: int = 6000,
    dpi: int = 110,
    linewidth: float = 1.6,
    basemap_zoom: int | str = "auto",
    label_zoom: int | None = None,
    simplify_eps: float = 1e-4,
) -> Path:
    """Render the clip. Returns the output path."""
    if not trips["departed_after"].dt.tz:
        raise ValueError("trips.departed_after must be tz-aware")
    print(
        f"render: {len(trips):,} trips, window "
        f"{window_start.isoformat()} → {window_end.isoformat()}, "
        f"time_scale={time_scale}s/s, fps={fps}, trail={trail_seconds}s"
    )

    # Prep arrays.
    segs, seg_trip_idx, seg_within, trip_start, trip_end = _prep_tracks(
        trips, routes, simplify_eps=simplify_eps
    )
    # Per-trip n_seg, useful for the visibility math.
    n_trips = len(trip_start)
    trip_nseg = np.zeros(n_trips, dtype=np.int32)
    np.add.at(trip_nseg, seg_trip_idx, 1)
    print(f"render: {len(segs):,} polyline segments across {n_trips:,} trips")

    # Precompute per-segment color ramp + alpha base. These never change
    # between frames; the per-frame work is the fade/visibility multiplication.
    pos = seg_within.astype(np.float32) / np.maximum(
        trip_nseg[seg_trip_idx].astype(np.float32) - 1.0, 1.0
    )
    rgb_pos = (TAIL_RGB[None, :] + (HEAD_RGB - TAIL_RGB)[None, :] * pos[:, None]).astype(np.float32)
    alpha_pos = (0.12 + 0.78 * pos).astype(np.float32)

    # Figure setup.
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=figsize, facecolor="black", dpi=dpi)
    ax.set_facecolor("black")
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])
    ax.set_aspect(1.0 / np.cos(np.radians((bbox[1] + bbox[3]) / 2)))
    ax.set_axis_off()
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    # Static basemap (slow part is here). `@2x` retina tiles give 2× pixel
    # density so the basemap stays sharp at 2K/4K output.
    # If `label_zoom` is set, draw a two-layer composite (NoLabels base +
    # OnlyLabels at a lower zoom for chunkier city names). Otherwise use the
    # combined DarkMatter — its labels look great at 4K but get small at lower
    # output resolutions.
    if label_zoom is None:
        print(
            f"render: drawing CartoDB Dark Matter basemap "
            f"(zoom={basemap_zoom} @2x)…"
        )
        cx.add_basemap(
            ax, crs="EPSG:4326",
            source=cx.providers.CartoDB.DarkMatter(r="@2x"),
            zoom=basemap_zoom, attribution=False, interpolation="bilinear",
        )
    else:
        print(
            f"render: drawing composite basemap "
            f"(base zoom={basemap_zoom} @2x + labels zoom={label_zoom} @2x)…"
        )
        cx.add_basemap(
            ax, crs="EPSG:4326",
            source=cx.providers.CartoDB.DarkMatterNoLabels(r="@2x"),
            zoom=basemap_zoom, attribution=False, interpolation="bilinear",
        )
        ax.set_xlim(bbox[0], bbox[2])
        ax.set_ylim(bbox[1], bbox[3])
        cx.add_basemap(
            ax, crs="EPSG:4326",
            source=cx.providers.CartoDB.DarkMatterOnlyLabels(r="@2x"),
            zoom=label_zoom, attribution=False, interpolation="bilinear",
        )
    # Re-clip after basemap (contextily may widen extents).
    ax.set_xlim(bbox[0], bbox[2])
    ax.set_ylim(bbox[1], bbox[3])

    # Segments don't change frame-to-frame — set them once. Round caps look
    # nicer but cost a bit; the joinstyle is irrelevant for free-floating
    # segments (each segment is its own polyline of length 2).
    lc = LineCollection(segs, linewidths=linewidth, capstyle="round")
    lc.set_animated(True)
    ax.add_collection(lc)

    # Frame timing.
    window_start_ts = window_start.timestamp()
    window_end_ts = window_end.timestamp()
    real_seconds = window_end_ts - window_start_ts
    video_seconds = real_seconds / time_scale
    n_frames = int(np.ceil(video_seconds * fps))
    print(
        f"render: {n_frames} frames → {video_seconds:.1f}s of video "
        f"({real_seconds / 3600:.1f}h of real time)"
    )

    def frame(i: int):
        t = window_start_ts + (i / fps) * time_scale
        colors = _seg_colors(
            t, trip_start, trip_end, seg_trip_idx, seg_within, trip_nseg,
            trail_seconds, rgb_pos=rgb_pos, alpha_pos=alpha_pos,
        )
        lc.set_colors(colors)
        return (lc,)

    pbar = tqdm(total=n_frames, desc="encode", unit="frame")

    def frame_with_progress(i: int):
        artists = frame(i)
        pbar.update(1)
        return artists

    anim = animation.FuncAnimation(
        fig, frame_with_progress, frames=n_frames, interval=1000 / fps, blit=True
    )
    writer = animation.FFMpegWriter(
        fps=fps,
        bitrate=bitrate,
        codec="libx264",
        extra_args=["-pix_fmt", "yuv420p", "-preset", "fast", "-movflags", "+faststart"],
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    anim.save(str(out_path), writer=writer, dpi=dpi)
    pbar.close()
    plt.close(fig)
    print(f"render: wrote {out_path}")
    return out_path

"""Vehicle paths between stops, cut out of the GTFS shapes.

Every hop of a pattern (two consecutive stops) gets the part of its first trip's shape between
the two stops, cut by ``shape_dist_traveled`` in ``stop_times.txt`` and ``shapes.txt``. A hop
without usable shape data, or whose cut is implausibly long, stays a straight line. The paths
are cached next to the network as ``hops.json``.
"""

from __future__ import annotations

import itertools
import json
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

import polars as pl

from fare_dodger.geo import distance_m
from fare_dodger.gtfs.load import read_table
from fare_dodger.network.timetable import Timetable

#: Bump whenever the cutting logic changes so cached paths are rebuilt.
HOPS_VERSION = 1
HOPS_FILE = "hops.json"
#: A cut longer than this many times the straight stop-to-stop distance (plus slack) is
#: treated as bad shape data and replaced by a straight line.
MAX_DETOUR = 3.0
MAX_DETOUR_SLACK_M = 300.0

LatLon = tuple[float, float]
#: Per pattern, per hop: the shape points between the two stops (empty: straight line).
Hops = list[list[list[LatLon]]]


def straight_hops(tt: Timetable) -> Hops:
    return [[[] for _ in range(len(tt.pattern_stops(p)) - 1)] for p in range(tt.n_patterns)]


def _point_at(points: list[LatLon], dists: list[float], d: float) -> LatLon:
    i = min(max(bisect_right(dists, d) - 1, 0), len(points) - 2)
    span = dists[i + 1] - dists[i]
    f = min(max((d - dists[i]) / span, 0.0), 1.0) if span > 0 else 0.0
    (lat0, lon0), (lat1, lon1) = points[i], points[i + 1]
    return lat0 + (lat1 - lat0) * f, lon0 + (lon1 - lon0) * f


def cut_shape(points: list[LatLon], dists: list[float], d0: float, d1: float) -> list[LatLon]:
    """The part of a shape from distance ``d0`` to ``d1``, ends interpolated."""
    lo, hi = bisect_right(dists, d0), bisect_right(dists, d1)
    inner = [points[i] for i in range(lo, hi) if d0 < dists[i] < d1]
    return [_point_at(points, dists, d0), *inner, _point_at(points, dists, d1)]


def _length_m(path: list[LatLon]) -> float:
    return sum(distance_m(*a, *b) for a, b in itertools.pairwise(path))


def _stop_distances(
    rows: list[tuple[str, float | None]], node_of: dict[str, int]
) -> list[tuple[int, float, float]] | None:
    """``(node, distance on arrival, distance on departure)`` per visited node, consecutive
    platforms of one node collapsed as in the network build; ``None`` if a distance is missing."""
    out: list[tuple[int, float, float]] = []
    for stop_id, d in rows:
        node = node_of.get(stop_id)
        if node is None:
            continue
        if d is None:
            return None
        if out and out[-1][0] == node:
            out[-1] = (node, out[-1][1], d)
        else:
            out.append((node, d, d))
    return out


def build_hops(feed_dir: Path, tt: Timetable) -> Hops:
    hops = straight_hops(tt)
    shapes_path, stop_times_path = feed_dir / "shapes.txt", feed_dir / "stop_times.txt"
    if not shapes_path.is_file():
        return hops
    stop_times_lf = pl.scan_csv(stop_times_path, infer_schema=False, encoding="utf8-lossy")
    if "shape_dist_traveled" not in stop_times_lf.collect_schema().names():
        return hops

    first_trip = {tt.trip_id[tt.pat_trip_ptr[p]]: p for p in range(tt.n_patterns)}
    trips = read_table(feed_dir, "trips", ["trip_id"], ["shape_id"]).filter(
        pl.col("trip_id").is_in(sorted(first_trip)) & pl.col("shape_id").is_not_null()
    )
    shape_of = dict(trips.iter_rows())
    if not shape_of:
        return hops
    shape_rows = (
        read_table(
            feed_dir,
            "shapes",
            ["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
            ["shape_dist_traveled"],
        )
        .filter(pl.col("shape_id").is_in(sorted(set(shape_of.values()))))
        .with_columns(
            pl.col("shape_pt_lat", "shape_pt_lon", "shape_dist_traveled").cast(pl.Float64),
            pl.col("shape_pt_sequence").cast(pl.Int32),
        )
        .sort("shape_id", "shape_pt_sequence")
    )
    shapes: dict[str, tuple[list[LatLon], list[float]]] = {}
    for shape_id, lats, lons, dists in (
        shape_rows.group_by("shape_id", maintain_order=True)
        .agg("shape_pt_lat", "shape_pt_lon", "shape_dist_traveled")
        .iter_rows()
    ):
        if len(lats) >= 2 and None not in dists:
            shapes[shape_id] = (list(zip(lats, lons, strict=True)), list(dists))

    stop_rows: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
    for trip_id, stop_id, d in (
        stop_times_lf.select("trip_id", "stop_id", "stop_sequence", "shape_dist_traveled")
        .filter(pl.col("trip_id").is_in(sorted(shape_of)))
        .with_columns(
            pl.col("stop_sequence").cast(pl.Int32),
            pl.col("shape_dist_traveled").cast(pl.Float64),
        )
        .sort("trip_id", "stop_sequence")
        .collect()
        .select("trip_id", "stop_id", "shape_dist_traveled")
        .iter_rows()
    ):
        stop_rows[trip_id].append((stop_id, d))

    node_of = {pid: n for n, pids in enumerate(tt.node_platforms) for pid in pids}
    for trip_id, shape_id in sorted(shape_of.items()):
        p = first_trip[trip_id]
        stops = tt.pattern_stops(p)
        visits = _stop_distances(stop_rows.get(trip_id, []), node_of)
        if shape_id not in shapes or visits is None or [v[0] for v in visits] != stops:
            continue
        points, dists = shapes[shape_id]
        for i, (a, b) in enumerate(itertools.pairwise(visits)):
            if b[1] <= a[2]:
                continue
            path = cut_shape(points, dists, a[2], b[1])
            na, nb = stops[i], stops[i + 1]
            ends = [(tt.node_lat[na], tt.node_lon[na]), (tt.node_lat[nb], tt.node_lon[nb])]
            straight = _length_m(ends)
            if _length_m([ends[0], *path, ends[1]]) <= MAX_DETOUR * straight + MAX_DETOUR_SLACK_M:
                hops[p][i] = [(round(lat, 6), round(lon, 6)) for lat, lon in path]
    return hops


def load_or_build_hops(feed_dir: Path, tt: Timetable, cache_dir: Path) -> Hops:
    """Cached hop paths for the network in ``cache_dir``, built on first use."""
    path = cache_dir / HOPS_FILE
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == HOPS_VERSION:
            return [[[(lat, lon) for lat, lon in hop] for hop in pat] for pat in data["hops"]]
    hops = build_hops(feed_dir, tt)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": HOPS_VERSION, "hops": hops}), encoding="utf-8")
    return hops

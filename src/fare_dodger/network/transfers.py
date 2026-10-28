"""Walking transfers between nearby station nodes."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from fare_dodger.geo import distance_m, metres_per_degree


@dataclass(frozen=True, slots=True)
class Footpath:
    from_node: int
    to_node: int
    duration_s: int
    distance_m: float


def walking_transfers(
    node_points: list[list[tuple[float, float]]],
    radius_m: float,
    speed_mps: float,
    detour_factor: float = 1.0,
) -> list[Footpath]:
    """Footpaths (both directions) between nodes whose closest platforms are within ``radius_m``.

    ``node_points[i]`` holds the ``(lat, lon)`` of node ``i``'s platforms. A uniform grid with
    cells of ``radius_m`` keeps this linear in practice instead of quadratic.
    """
    if radius_m <= 0 or not node_points:
        return []
    ref_lat = sum(pts[0][0] for pts in node_points) / len(node_points)
    m_lat, m_lon = metres_per_degree(ref_lat)

    grid: dict[tuple[int, int], set[int]] = defaultdict(set)
    for node, pts in enumerate(node_points):
        for lat, lon in pts:
            grid[(math.floor(lat * m_lat / radius_m), math.floor(lon * m_lon / radius_m))].add(node)

    result: list[Footpath] = []
    for node, pts in enumerate(node_points):
        candidates: set[int] = set()
        for lat, lon in pts:
            cx, cy = math.floor(lat * m_lat / radius_m), math.floor(lon * m_lon / radius_m)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidates |= grid.get((cx + dx, cy + dy), set())
        for other in sorted(candidates):
            if other == node:
                continue
            dist = min(
                distance_m(a_lat, a_lon, b_lat, b_lon)
                for a_lat, a_lon in pts
                for b_lat, b_lon in node_points[other]
            )
            if dist <= radius_m:
                duration = max(1, math.ceil(dist * detour_factor / speed_mps))
                result.append(Footpath(node, other, duration, dist))
    return result

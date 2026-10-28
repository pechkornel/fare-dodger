"""Small-area geometry: distances and playable-area shapes.

The playable area spans a few kilometres, so an equirectangular projection around the
area's latitude is accurate to well under a metre and much cheaper than haversine.
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_008.8
_M_PER_DEG_LAT = math.pi * EARTH_RADIUS_M / 180.0


def metres_per_degree(lat: float) -> tuple[float, float]:
    """Metres per degree of latitude and of longitude at latitude ``lat``."""
    return _M_PER_DEG_LAT, _M_PER_DEG_LAT * math.cos(math.radians(lat))


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate ground distance in metres between two nearby points."""
    m_lat, m_lon = metres_per_degree((lat1 + lat2) / 2.0)
    return math.hypot((lat2 - lat1) * m_lat, (lon2 - lon1) * m_lon)


def point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting test; ``polygon`` is a list of ``(lat, lon)`` vertices (closed implicitly)."""
    inside = False
    n = len(polygon)
    for i in range(n):
        lat_a, lon_a = polygon[i]
        lat_b, lon_b = polygon[(i + 1) % n]
        if (lat_a > lat) != (lat_b > lat):
            cross_lon = lon_a + (lat - lat_a) * (lon_b - lon_a) / (lat_b - lat_a)
            if lon < cross_lon:
                inside = not inside
    return inside

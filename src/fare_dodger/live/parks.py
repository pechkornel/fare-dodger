"""Open ground: parks, lawns and squares where walking is free, not just along the paths.

Public parks, gardens, playgrounds, lawns and pedestrian squares are downloaded once from
OpenStreetMap (Overpass, ``out geom``) and cached as JSON. Each district keeps the shapes that
overlap its play area as polygons on its flat projection. Relations contribute their closed
outer rings; holes are ignored (a pond in a park is walkable).
"""

from __future__ import annotations

import itertools
import json
import math
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import httpx

from fare_dodger.geo import point_in_polygon
from fare_dodger.live.streets import BBox, overpass

#: Bump when the query changes so cached downloads are refreshed.
PARKS_VERSION = 1
#: Spatial index cell size (metres).
CELL_M = 100.0
#: Shapes smaller than this are left out (verges, planters), in square metres.
MIN_AREA_M2 = 150.0

Point = tuple[float, float]
LatLon = tuple[float, float]


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    box = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    public = '["access"!~"^(private|no)$"]'
    return (
        "[out:json][timeout:120];("
        f'way["leisure"~"^(park|garden|common|playground|dog_park)$"]{public}{box};'
        f'relation["leisure"~"^(park|garden|common)$"]{public}{box};'
        f'way["landuse"~"^(grass|village_green|recreation_ground)$"]{public}{box};'
        f'way["highway"="pedestrian"]["area"="yes"]{box};'
        f'way["place"="square"]{box};'
        ");out geom;"
    )


def _rings(element: dict[str, object]) -> list[list[LatLon]]:
    """Closed rings of a way, or the closed outer member ways of a relation."""

    def ring(geometry: object) -> list[LatLon] | None:
        if not isinstance(geometry, list) or len(geometry) < 4:
            return None
        points = [(float(g["lat"]), float(g["lon"])) for g in geometry]
        return points if points[0] == points[-1] else None

    if element.get("type") == "way":
        r = ring(element.get("geometry"))
        return [r] if r else []
    members = element.get("members")
    out = []
    for m in members if isinstance(members, list) else []:
        if m.get("type") == "way" and m.get("role", "outer") == "outer":
            r = ring(m.get("geometry"))
            if r:
                out.append(r)
    return out


def download_parks(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the open ground in ``bbox`` from Overpass into ``path`` (JSON)."""
    data = overpass(overpass_query(bbox), client or httpx.Client(timeout=httpx.Timeout(180.0)))
    polygons = [
        [[round(lat, 7), round(lon, 7)] for lat, lon in r]
        for el in data["elements"]
        for r in _rings(el)
    ]
    payload = {"version": PARKS_VERSION, "bbox": list(bbox), "polygons": polygons}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def read_parks(path: Path, bbox: BBox | None = None) -> list[list[LatLon]] | None:
    """Polygons (``[(lat, lon), ...]``) from a cached download, or ``None`` if the file is
    missing, from another query version, or does not cover ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != PARKS_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    return [[(lat, lon) for lat, lon in poly] for poly in data["polygons"]]


def _area(poly: list[Point]) -> float:
    return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in itertools.pairwise(poly))) / 2


class Parks:
    """Open-ground polygons on a flat projection, with a grid index for point tests."""

    def __init__(self, polygons: list[list[Point]]) -> None:
        self.polygons = [p for p in polygons if len(p) >= 4 and _area(p) >= MIN_AREA_M2]
        # point_in_polygon takes (lat, lon)-ordered pairs: keep the polygons as (y, x).
        self._yx = [[(y, x) for x, y in poly] for poly in self.polygons]
        self._cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, poly in enumerate(self.polygons):
            xs = [x for x, _ in poly]
            ys = [y for _, y in poly]
            for cx in range(self._cell(min(xs)), self._cell(max(xs)) + 1):
                for cy in range(self._cell(min(ys)), self._cell(max(ys)) + 1):
                    self._cells[(cx, cy)].append(i)

    @classmethod
    def from_latlon(
        cls,
        polygons: list[list[LatLon]],
        to_xy: Callable[[float, float], Point],
        inside: Callable[[float, float], bool],
    ) -> Parks:
        """The polygons with a corner inside the area (``inside(lat, lon)``)."""
        return cls(
            [
                [to_xy(lat, lon) for lat, lon in p]
                for p in polygons
                if any(inside(lat, lon) for lat, lon in p)
            ]
        )

    @staticmethod
    def _cell(v: float) -> int:
        return math.floor(v / CELL_M)

    def __len__(self) -> int:
        return len(self.polygons)

    def contains(self, p: Point) -> bool:
        return any(
            point_in_polygon(p[1], p[0], self._yx[i])
            for i in self._cells.get((self._cell(p[0]), self._cell(p[1])), ())
        )

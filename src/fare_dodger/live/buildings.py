"""Building footprints: the only things a walking player cannot pass through.

Footprints (``building=*`` ways and multipolygon relations) are downloaded once from the
Overpass API for a bounding box and cached as JSON. Relations contribute their outer rings,
stitched together from their member ways. Canopies and shelters (``building=roof``), stations
and ruins, and anything underground do not block. Each district keeps the footprints with a
corner inside its play area, on its flat projection, minus any footprint that contains a stop,
a station or a café, so those can always be reached.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from fare_dodger.geo import point_in_polygon
from fare_dodger.live.streets import BBox, overpass

#: Bump when the query or the filtering changes so cached downloads are refreshed.
BUILDINGS_VERSION = 1
#: Spatial index cell size (metres).
CELL_M = 50.0
#: Footprints that never block.
OPEN_KINDS = frozenset(
    {"roof", "train_station", "transportation", "station", "ruins", "construction", "no"}
)

Point = tuple[float, float]
LatLon = tuple[float, float]


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    box = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    return f'[out:json][timeout:240];(way["building"]{box};relation["building"]{box};);out geom;'


def _blocks(tags: dict[str, Any]) -> bool:
    if tags.get("building") in OPEN_KINDS:
        return False
    if tags.get("location") == "underground":
        return False
    layer = str(tags.get("layer", "0"))
    return not (layer.startswith("-") and layer[1:].isdigit())


def _points(geometry: object) -> list[LatLon]:
    if not isinstance(geometry, list):
        return []
    return [(float(g["lat"]), float(g["lon"])) for g in geometry if g]


def _stitch(parts: list[list[LatLon]]) -> list[list[LatLon]]:
    """Join open way segments end to end into closed rings (unclosable leftovers are dropped)."""
    rings = [p for p in parts if len(p) >= 4 and p[0] == p[-1]]
    open_parts = [p for p in parts if len(p) >= 2 and p[0] != p[-1]]
    while open_parts:
        ring = open_parts.pop()
        progress = True
        while ring[0] != ring[-1] and progress:
            progress = False
            for i, part in enumerate(open_parts):
                if part[0] == ring[-1]:
                    ring = ring + part[1:]
                elif part[-1] == ring[-1]:
                    ring = ring + part[::-1][1:]
                else:
                    continue
                open_parts.pop(i)
                progress = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def footprints(elements: list[dict[str, Any]]) -> list[list[LatLon]]:
    """Blocking footprints (closed rings) from Overpass ``out geom`` elements."""
    out: list[list[LatLon]] = []
    for el in elements:
        if not _blocks(el.get("tags", {})):
            continue
        if el.get("type") == "way":
            ring = _points(el.get("geometry"))
            if len(ring) >= 4 and ring[0] == ring[-1]:
                out.append(ring)
        elif el.get("type") == "relation":
            members = el.get("members", [])
            outer = [
                _points(m.get("geometry"))
                for m in members
                if m.get("type") == "way" and m.get("role", "outer") in ("outer", "")
            ]
            out.extend(_stitch(outer))
    return out


def download_buildings(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the building footprints in ``bbox`` from Overpass into ``path`` (JSON)."""
    data = overpass(overpass_query(bbox), client or httpx.Client(timeout=httpx.Timeout(300.0)))
    polygons = [
        [[round(lat, 7), round(lon, 7)] for lat, lon in r] for r in footprints(data["elements"])
    ]
    payload = {"version": BUILDINGS_VERSION, "bbox": list(bbox), "polygons": polygons}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def read_buildings(path: Path, bbox: BBox | None = None) -> list[list[LatLon]] | None:
    """Footprints (``[(lat, lon), ...]``) from a cached download, or ``None`` if the file is
    missing, from another version, or does not cover ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != BUILDINGS_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    return [[(lat, lon) for lat, lon in poly] for poly in data["polygons"]]


class Buildings:
    """Blocking footprints on a flat projection, with a grid index for point tests."""

    def __init__(self, polygons: list[list[Point]], keep_open: list[Point] | None = None) -> None:
        keep_open = keep_open or []
        candidates = [p for p in polygons if len(p) >= 4]
        # point_in_polygon takes (lat, lon)-ordered pairs: keep the polygons as (y, x).
        yx = [[(y, x) for x, y in poly] for poly in candidates]
        boxes = [
            (min(x for x, _ in p), min(y for _, y in p), max(x for x, _ in p), max(y for _, y in p))
            for p in candidates
        ]

        def holds(i: int, q: Point) -> bool:
            x0, y0, x1, y1 = boxes[i]
            return x0 <= q[0] <= x1 and y0 <= q[1] <= y1 and point_in_polygon(q[1], q[0], yx[i])

        keep = [i for i in range(len(candidates)) if not any(holds(i, q) for q in keep_open)]
        self.polygons = [candidates[i] for i in keep]
        self._yx = [yx[i] for i in keep]
        self._boxes = [boxes[i] for i in keep]
        self._cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (x0, y0, x1, y1) in enumerate(self._boxes):
            for cx in range(self._cell(x0), self._cell(x1) + 1):
                for cy in range(self._cell(y0), self._cell(y1) + 1):
                    self._cells[(cx, cy)].append(i)

    @classmethod
    def from_latlon(
        cls,
        polygons: list[list[LatLon]],
        to_xy: Callable[[float, float], Point],
        inside: Callable[[float, float], bool],
        keep_open: list[Point],
    ) -> Buildings:
        """The footprints with a corner inside the area (``inside(lat, lon)``), except those
        containing one of ``keep_open`` (stops, stations, cafés)."""
        return cls(
            [
                [to_xy(lat, lon) for lat, lon in p]
                for p in polygons
                if any(inside(lat, lon) for lat, lon in p)
            ],
            keep_open,
        )

    @staticmethod
    def _cell(v: float) -> int:
        return math.floor(v / CELL_M)

    def __len__(self) -> int:
        return len(self.polygons)

    def at(self, p: Point) -> int | None:
        """The footprint containing ``p``, if any."""
        for i in self._cells.get((self._cell(p[0]), self._cell(p[1])), ()):
            x0, y0, x1, y1 = self._boxes[i]
            if x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and point_in_polygon(p[1], p[0], self._yx[i]):
                return i
        return None

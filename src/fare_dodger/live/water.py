"""Water: the Danube and the city's ponds are walls; bridges cross them.

Water areas (``natural=water`` and ``waterway=riverbank`` ways and multipolygon relations,
with their islands as inner rings) and road and foot bridges (``highway`` ways tagged
``bridge``) are downloaded from Overpass per district and cached as JSON. Each district turns
them into a raster on its flat projection (``CELL_M`` cells): a cell is water when it lies
inside an outer ring and outside its islands (even-odd per area), unless a bridge passes
within ``BRIDGE_HALF_WIDTH_M``. Looking a point up is then a single array read.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from fare_dodger.live.buildings import _points, _stitch
from fare_dodger.live.streets import BBox, overpass

#: Bump when the query or the format changes so cached downloads are refreshed.
WATER_VERSION = 1
CELL_M = 4.0
#: Half the walkable width of a bridge (metres): carriageway and pavements.
BRIDGE_HALF_WIDTH_M = 9.0

Point = tuple[float, float]
LatLon = tuple[float, float]
Ring = list[LatLon]


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    box = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    return (
        "[out:json][timeout:180];("
        f'relation["natural"="water"]{box};way["natural"="water"]{box};'
        f'relation["waterway"="riverbank"]{box};way["waterway"="riverbank"]{box};'
        f'way["highway"]["bridge"]["bridge"!="no"]{box};'
        ");out geom;"
    )


def parse(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """Water areas (outer rings and islands) and bridge lines from ``out geom`` elements."""
    areas: list[dict[str, list[Ring]]] = []
    bridges: list[Ring] = []
    for el in elements:
        tags = el.get("tags", {})
        if "highway" in tags:
            line = _points(el.get("geometry"))
            if len(line) >= 2:
                bridges.append(line)
            continue
        if el.get("type") == "way":
            ring = _points(el.get("geometry"))
            if len(ring) >= 4 and ring[0] == ring[-1]:
                areas.append({"outer": [ring], "inner": []})
        elif el.get("type") == "relation":
            members = el.get("members", [])

            def rings(role: str, members: list[dict[str, Any]] = members) -> list[Ring]:
                return _stitch(
                    [
                        _points(m.get("geometry"))
                        for m in members
                        if m.get("type") == "way" and m.get("role", "outer") == role
                    ]
                )

            outer = rings("outer")
            if outer:
                areas.append({"outer": outer, "inner": rings("inner")})
    return {"areas": areas, "bridges": bridges}


def download_water(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the water and bridges around ``bbox`` from Overpass into ``path`` (JSON)."""
    data = overpass(overpass_query(bbox), client or httpx.Client(timeout=httpx.Timeout(240.0)))
    payload = {"version": WATER_VERSION, "bbox": list(bbox), **parse(data["elements"])}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return path


def read_water(path: Path, bbox: BBox | None = None) -> dict[str, Any] | None:
    """Areas and bridges from a cached download, or ``None`` if missing, outdated or not
    covering ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != WATER_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    return {"areas": data["areas"], "bridges": data["bridges"]}


def _fill(mask: np.ndarray, rings: list[list[Point]], x0: float, y0: float, cell: float) -> None:
    """Set the cells whose centres lie inside ``rings`` (even-odd, so islands stay out)."""
    edges = [(a, b) for ring in rings for a, b in itertools.pairwise(ring) if a[1] != b[1]]
    if not edges:
        return
    ax = np.array([a[0] for a, _ in edges])
    ay = np.array([a[1] for a, _ in edges])
    bx = np.array([b[0] for _, b in edges])
    by = np.array([b[1] for _, b in edges])
    rows, cols = mask.shape
    low = max(0, math.floor((min(ay.min(), by.min()) - y0) / cell))
    high = min(rows - 1, math.ceil((max(ay.max(), by.max()) - y0) / cell))
    for r in range(low, high + 1):
        yc = y0 + (r + 0.5) * cell
        crossing = (ay <= yc) != (by <= yc)
        if not crossing.any():
            continue
        xs = np.sort(
            ax[crossing]
            + (yc - ay[crossing]) * (bx[crossing] - ax[crossing]) / (by[crossing] - ay[crossing])
        )
        row = np.zeros(cols, dtype=bool)
        for start, end in zip(xs[0::2], xs[1::2], strict=False):
            c0 = max(0, math.ceil((start - x0) / cell - 0.5))
            c1 = min(cols - 1, math.floor((end - x0) / cell - 0.5))
            if c0 <= c1:
                row[c0 : c1 + 1] = True
        mask[r] |= row


class WaterMask:
    """Water on a grid over ``bounds`` (x0, y0, x1, y1), with bridges cut out of it."""

    def __init__(
        self,
        areas: list[tuple[list[list[Point]], list[list[Point]]]],
        bridges: list[list[Point]],
        bounds: tuple[float, float, float, float],
        cell: float = CELL_M,
    ) -> None:
        x0, y0, x1, y1 = bounds
        self.x0, self.y0, self.cell = x0, y0, cell
        cols, rows = max(1, math.ceil((x1 - x0) / cell)), max(1, math.ceil((y1 - y0) / cell))
        self.water = np.zeros((rows, cols), dtype=bool)
        for outer, inner in areas:
            area = np.zeros_like(self.water)
            _fill(area, outer + inner, x0, y0, cell)
            self.water |= area
        reach = math.ceil(BRIDGE_HALF_WIDTH_M / cell)
        for line in bridges:
            for (ax, ay), (bx, by) in itertools.pairwise(line):
                steps = max(1, math.ceil(math.hypot(bx - ax, by - ay) / (cell / 2)))
                for k in range(steps + 1):
                    px, py = ax + (bx - ax) * k / steps, ay + (by - ay) * k / steps
                    c, r = int((px - x0) / cell), int((py - y0) / cell)
                    r0, r1 = max(0, r - reach), min(rows, r + reach + 1)
                    c0, c1 = max(0, c - reach), min(cols, c + reach + 1)
                    if r0 < r1 and c0 < c1:
                        self.water[r0:r1, c0:c1] = False

    @classmethod
    def from_latlon(
        cls,
        data: dict[str, Any],
        to_xy: Callable[[float, float], Point],
        bounds: tuple[float, float, float, float],
    ) -> WaterMask:
        def ring(points: list[list[float]]) -> list[Point]:
            return [to_xy(lat, lon) for lat, lon in points]

        areas = [
            ([ring(r) for r in a["outer"]], [ring(r) for r in a["inner"]]) for a in data["areas"]
        ]
        return cls(areas, [ring(line) for line in data["bridges"]], bounds)

    def blocks(self, p: Point) -> bool:
        c = int((p[0] - self.x0) / self.cell)
        r = int((p[1] - self.y0) / self.cell)
        rows, cols = self.water.shape
        return 0 <= r < rows and 0 <= c < cols and bool(self.water[r, c])

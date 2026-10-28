"""Walking on real streets: an OpenStreetMap street graph for the live game.

Walkable ways (every ``highway`` except motorways and the like, minus ``foot=no`` and private
access) are downloaded once from the Overpass API for a bounding box and cached as JSON. Each
district builds a :class:`StreetGraph` from them: the edges with both ends inside the play
area, largest connected component only, on the district's flat projection.

Positions on the graph are :class:`EdgePos` values (an edge ``a -> b`` and the distance ``s``
from ``a``). The human steers with a direction (:meth:`StreetGraph.move`): it follows the
street that best matches the direction, takes bends on its own, and turns at the next
junction when the direction points across the current street. Bots follow shortest street
routes (:meth:`StreetGraph.route`, :class:`RouteFollower`).
"""

from __future__ import annotations

import heapq
import itertools
import json
import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from fare_dodger.config import Area, BBoxArea, CircleArea
from fare_dodger.geo import metres_per_degree

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
#: Public Overpass instances, tried in order when one is down or overloaded.
OVERPASS_MIRRORS = (
    OVERPASS_URL,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "fare-dodger (https://github.com/pechkornel/fare-dodger)"
_EXCLUDED = (
    "motorway|motorway_link|construction|proposed|bus_guideway|raceway|elevator|platform|"
    "abandoned|razed"
)
#: Bump when the query changes so cached downloads are refreshed.
STREETS_VERSION = 1
#: Spatial index cell size (metres).
CELL_M = 50.0
_EPS = 1e-6
#: Dot product with the steering direction needed to take a street at a junction.
TURN_DOT = 0.35
#: At a bend (two streets meet), keep going unless the street turns back this sharply.
BEND_DOT = -0.2
#: Inside an edge, a direction this far off the street means "turn at the next junction".
ALONG_DOT = 0.2

Point = tuple[float, float]
BBox = tuple[float, float, float, float]  # south, west, north, east


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    return (
        "[out:json][timeout:180];"
        f'way["highway"]["highway"!~"^({_EXCLUDED})$"]["foot"!~"^(no|private)$"]'
        f'["access"!~"^(private|no)$"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
        "(._;>;);out skel qt;"
    )


def overpass(query: str, client: httpx.Client) -> dict[str, Any]:
    """Run an Overpass query, trying each mirror until one answers."""
    error: httpx.HTTPError | None = None
    for url in OVERPASS_MIRRORS:
        try:
            response = client.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPError as exc:
            error = exc
    assert error is not None
    raise error


def download_streets(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the walkable ways in ``bbox`` from Overpass into ``path`` (JSON)."""
    data = overpass(overpass_query(bbox), client or httpx.Client(timeout=httpx.Timeout(300.0)))
    payload = {"version": STREETS_VERSION, "bbox": list(bbox), "elements": data["elements"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def read_streets(
    path: Path, bbox: BBox | None = None
) -> tuple[dict[int, tuple[float, float]], list[list[int]]] | None:
    """OSM nodes (id -> lat, lon) and ways (node ids) from a cached download, or ``None`` if
    the file is missing, from another query version, or does not cover ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != STREETS_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    nodes: dict[int, tuple[float, float]] = {}
    ways: list[list[int]] = []
    for el in data["elements"]:
        if el["type"] == "node":
            nodes[el["id"]] = (el["lat"], el["lon"])
        elif el["type"] == "way":
            ways.append(el["nodes"])
    return nodes, ways


def area_bbox(area: Area) -> BBox:
    if isinstance(area, BBoxArea):
        return area.min_lat, area.min_lon, area.max_lat, area.max_lon
    if isinstance(area, CircleArea):
        m_lat, m_lon = metres_per_degree(area.center_lat)
        dlat, dlon = area.radius_m / m_lat, area.radius_m / m_lon
        return (
            area.center_lat - dlat,
            area.center_lon - dlon,
            area.center_lat + dlat,
            area.center_lon + dlon,
        )
    lats = [v[0] for v in area.vertices]
    lons = [v[1] for v in area.vertices]
    return min(lats), min(lons), max(lats), max(lons)


def covering_bbox(boxes: list[BBox], margin_deg: float = 0.002) -> BBox:
    return (
        min(b[0] for b in boxes) - margin_deg,
        min(b[1] for b in boxes) - margin_deg,
        max(b[2] for b in boxes) + margin_deg,
        max(b[3] for b in boxes) + margin_deg,
    )


@dataclass(frozen=True, slots=True)
class EdgePos:
    a: int
    b: int
    #: Distance from ``a`` along the edge.
    s: float


def _unit(p: Point, q: Point) -> Point:
    dx, dy = q[0] - p[0], q[1] - p[1]
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 0 else (0.0, 0.0)


def _dot(u: Point, v: Point) -> float:
    return u[0] * v[0] + u[1] * v[1]


class StreetGraph:
    def __init__(self, xy: list[Point], edges: list[tuple[int, int]]) -> None:
        self.xy = xy
        adj: list[set[int]] = [set() for _ in xy]
        for a, b in edges:
            if a != b:
                adj[a].add(b)
                adj[b].add(a)
        self.adj = [sorted(n) for n in adj]
        self.edges = sorted({(min(a, b), max(a, b)) for a, b in edges if a != b})
        self._cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i, (a, b) in enumerate(self.edges):
            (x0, y0), (x1, y1) = xy[a], xy[b]
            for cx in range(self._cell(min(x0, x1)), self._cell(max(x0, x1)) + 1):
                for cy in range(self._cell(min(y0, y1)), self._cell(max(y0, y1)) + 1):
                    self._cells[(cx, cy)].append(i)
        self._path_cache: dict[tuple[Point, Point], list[Point]] = {}

    @classmethod
    def from_osm(
        cls,
        nodes: dict[int, tuple[float, float]],
        ways: list[list[int]],
        to_xy: Callable[[float, float], Point],
        inside: Callable[[float, float], bool],
    ) -> StreetGraph:
        """The graph of ``ways`` inside the area (``inside(lat, lon)``), largest component."""
        keep = {nid for nid, (lat, lon) in nodes.items() if inside(lat, lon)}
        pairs = [
            (u, v) for way in ways for u, v in itertools.pairwise(way) if u in keep and v in keep
        ]
        adj: dict[int, list[int]] = defaultdict(list)
        for u, v in pairs:
            adj[u].append(v)
            adj[v].append(u)
        best: list[int] = []
        seen: set[int] = set()
        for start in sorted(adj):
            if start in seen:
                continue
            component, stack = [], [start]
            seen.add(start)
            while stack:
                n = stack.pop()
                component.append(n)
                for m in adj[n]:
                    if m not in seen:
                        seen.add(m)
                        stack.append(m)
            if len(component) > len(best):
                best = component
        ids = sorted(best)
        index = {nid: i for i, nid in enumerate(ids)}
        xy = [to_xy(*nodes[nid]) for nid in ids]
        edges = [(index[u], index[v]) for u, v in pairs if u in index and v in index]
        return cls(xy, edges)

    @staticmethod
    def _cell(v: float) -> int:
        return math.floor(v / CELL_M)

    def __len__(self) -> int:
        return len(self.xy)

    def length(self, a: int, b: int) -> float:
        return math.dist(self.xy[a], self.xy[b])

    def point(self, ep: EdgePos) -> Point:
        (x0, y0), (x1, y1) = self.xy[ep.a], self.xy[ep.b]
        length = self.length(ep.a, ep.b)
        f = ep.s / length if length > 0 else 0.0
        return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f

    # --- snapping --------------------------------------------------------------------------

    def snap(self, p: Point) -> EdgePos:
        """The closest point on the graph to ``p``."""
        cx, cy = self._cell(p[0]), self._cell(p[1])
        best: tuple[float, EdgePos] | None = None
        ring = 0
        while True:
            for gx in range(cx - ring, cx + ring + 1):
                for gy in range(cy - ring, cy + ring + 1):
                    if max(abs(gx - cx), abs(gy - cy)) != ring:
                        continue
                    for i in self._cells.get((gx, gy), ()):
                        a, b = self.edges[i]
                        d, s = self._project(p, a, b)
                        if best is None or d < best[0]:
                            best = (d, EdgePos(a, b, s))
            # Every edge within ``ring * CELL_M`` of ``p`` has been seen.
            if best is not None and best[0] <= ring * CELL_M:
                return best[1]
            ring += 1
            if ring > 10_000:
                msg = "empty street graph"
                raise ValueError(msg)

    def _project(self, p: Point, a: int, b: int) -> tuple[float, float]:
        (x0, y0), (x1, y1) = self.xy[a], self.xy[b]
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy
        f = (
            0.0
            if length2 == 0
            else max(0.0, min(1.0, ((p[0] - x0) * dx + (p[1] - y0) * dy) / length2))
        )
        q = (x0 + dx * f, y0 + dy * f)
        return math.dist(p, q), f * math.sqrt(length2)

    # --- steering --------------------------------------------------------------------------

    def _best_turn(
        self, node: int, direction: Point, exclude: int | None = None
    ) -> tuple[int, float] | None:
        options = [m for m in self.adj[node] if m != exclude]
        if not options:
            return None
        here = self.xy[node]
        scored = [(_dot(direction, _unit(here, self.xy[m])), -m) for m in options]
        dot, neg = max(scored)
        return -neg, dot

    def _choose(self, node: int, direction: Point, came: int | None) -> int | None:
        """The street to take at ``node``: the one best matching ``direction``; or, arriving
        at a bend from ``came`` (only two streets meet), the way on unless it turns back."""
        best = self._best_turn(node, direction)
        if best is None:
            return None
        nxt, dot = best
        if dot >= TURN_DOT:
            return nxt
        if came is not None and len(self.adj[node]) == 2:
            onward = self._best_turn(node, direction, exclude=came)
            if onward is not None and onward[1] >= BEND_DOT:
                return onward[0]
        return None

    def move(self, ep: EdgePos, direction: Point, dist: float) -> EdgePos:
        """Walk up to ``dist`` metres from ``ep``, steering with the unit vector ``direction``."""
        if direction == (0.0, 0.0):
            return ep
        a, b, s = ep.a, ep.b, ep.s
        for _ in range(256):
            if dist <= _EPS:
                break
            length = self.length(a, b)
            came = None
            if s >= length - _EPS:  # arrived at b, coming from a
                a, b, s, came = b, a, 0.0, a
            if s <= _EPS:  # at node a: pick the street to take
                nxt = self._choose(a, direction, came)
                if nxt is None:
                    return EdgePos(a, b, 0.0)
                b, s = nxt, 0.0
            else:  # inside the edge: go along it, or towards the junction to turn at
                along = _dot(direction, _unit(self.xy[a], self.xy[b]))
                if along <= -ALONG_DOT:
                    a, b, s = b, a, length - s
                elif along < ALONG_DOT:
                    ahead = self._best_turn(b, direction, exclude=a)
                    behind = self._best_turn(a, direction, exclude=b)
                    dot_ahead = ahead[1] if ahead else -2.0
                    dot_behind = behind[1] if behind else -2.0
                    if max(dot_ahead, dot_behind) < TURN_DOT:
                        # Mid-bend (the edge points the way we were heading, towards b):
                        # keep going round it rather than stopping.
                        if not (len(self.adj[b]) == 2 and dot_ahead >= BEND_DOT):
                            break
                    elif dot_behind > dot_ahead:
                        a, b, s = b, a, length - s
            length = self.length(a, b)
            step = min(dist, length - s)
            s += step
            dist -= step
        return EdgePos(a, b, s)

    # --- routing ---------------------------------------------------------------------------

    def route(self, p: Point, q: Point) -> list[Point]:
        """A shortest street route from ``p`` to ``q``: from ``p``'s closest street point to
        ``q``'s (both ends included; ``p`` and ``q`` themselves are not)."""
        sp, sq = self.snap(p), self.snap(q)
        start, goal = self.point(sp), self.point(sq)
        if {sp.a, sp.b} == {sq.a, sq.b}:
            return [start, goal]
        goal_cost = {sq.a: sq.s, sq.b: self.length(sq.a, sq.b) - sq.s}
        xy = self.xy
        dist: dict[int, float] = {sp.a: sp.s, sp.b: self.length(sp.a, sp.b) - sp.s}
        prev: dict[int, int | None] = {sp.a: None, sp.b: None}
        heap = [(d + math.dist(xy[n], goal), d, n) for n, d in dist.items()]
        heapq.heapify(heap)
        done: set[int] = set()
        best_end: tuple[float, int] | None = None
        while heap:
            _, d, n = heapq.heappop(heap)
            if n in done:
                continue
            done.add(n)
            if best_end is not None and d >= best_end[0]:
                break
            if n in goal_cost:
                total = d + goal_cost[n]
                if best_end is None or total < best_end[0]:
                    best_end = (total, n)
            for m in self.adj[n]:
                nd = d + math.dist(xy[n], xy[m])
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd + math.dist(xy[m], goal), nd, m))
        if best_end is None:
            return [start, goal]
        nodes: list[int] = []
        cur: int | None = best_end[1]
        while cur is not None:
            nodes.append(cur)
            cur = prev.get(cur)
        nodes.reverse()
        return [start, *(xy[n] for n in nodes), goal]

    def station_path(self, a: Point, b: Point, key: tuple[Point, Point]) -> list[Point]:
        """Cached walking path between two stations: ``a``, the street route, then ``b``."""
        path = self._path_cache.get(key)
        if path is None:
            path = [a, *self.route(a, b), b]
            self._path_cache[key] = path
        return path


def along(path: list[Point], f: float) -> Point:
    """The point a fraction ``f`` of the way along the polyline ``path``."""
    if len(path) == 1:
        return path[0]
    lengths = [math.dist(p, q) for p, q in itertools.pairwise(path)]
    target = max(0.0, min(1.0, f)) * sum(lengths)
    for (p, q), length in zip(itertools.pairwise(path), lengths, strict=True):
        if target <= length and length > 0:
            g = target / length
            return p[0] + (q[0] - p[0]) * g, p[1] + (q[1] - p[1]) * g
        target -= length
    return path[-1]


@dataclass
class RouteFollower:
    """Walks a street route towards a target, rerouting when the target moves away."""

    #: Reroute when the target is this far from the one the route was made for.
    reroute_m: float = 25.0
    target: Point | None = None
    path: list[Point] = field(default_factory=list)

    def step(self, graph: StreetGraph, pos: Point, target: Point, dist: float) -> Point:
        if self.target is None or math.dist(self.target, target) > self.reroute_m or not self.path:
            self.target = target
            self.path = [*graph.route(pos, target), target]
        while dist > 0 and self.path:
            nxt = self.path[0]
            gap = math.dist(pos, nxt)
            if gap <= dist:
                pos, dist = nxt, dist - gap
                self.path.pop(0)
            else:
                f = dist / gap
                return pos[0] + (nxt[0] - pos[0]) * f, pos[1] + (nxt[1] - pos[1]) * f
        return pos

    def reset(self) -> None:
        self.target, self.path = None, []

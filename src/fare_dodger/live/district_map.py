"""Pictures and facts of a district, for the docs (``fare-dodger districts``).

:func:`district_svg` draws a district as a small SVG map: its outline, water, streets, the
transit lines in the client's colours, stations, cafés and the jail. :func:`district_facts`
gives the numbers the docs table shows.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from fare_dodger.config import CircleArea
from fare_dodger.live.balance import TARGET, Outcome, wilson
from fare_dodger.live.streets import area_bbox
from fare_dodger.live.world import Point, World
from fare_dodger.modes import Mode

#: The client's line colours (static/app.js), with the metro lines in their own.
MODE_COLORS = {
    Mode.METRO: "#7950f2",
    Mode.TRAM: "#fcc419",
    Mode.TROLLEYBUS: "#fd7e14",
    Mode.BUS: "#339af0",
    Mode.SUBURBAN_RAIL: "#2f9e44",
    Mode.FERRY: "#15aabf",
}
METRO_COLORS = {"M1": "#f5c400", "M2": "#e41f18", "M3": "#005ca5", "M4": "#3fa535"}
#: Drawing order and line width (px): buses at the bottom, the metro on top.
LINE_STYLE = {
    Mode.BUS: 1.2,
    Mode.TROLLEYBUS: 1.4,
    Mode.FERRY: 1.6,
    Mode.TRAM: 2.0,
    Mode.SUBURBAN_RAIL: 2.4,
    Mode.METRO: 3.4,
}
#: Water is drawn in blocks of this many metres.
WATER_BLOCK_M = 24.0
#: Lines are simplified to this many pixels.
SIMPLIFY_PX = 0.7

Pixel = tuple[int, int]


def simplify(points: list[tuple[float, float]], tol: float = SIMPLIFY_PX) -> list[Pixel]:
    """Douglas-Peucker to ``tol`` pixels, then whole pixels without repeats."""
    if len(points) <= 2:
        kept = points
    else:
        keep = [False] * len(points)
        keep[0] = keep[-1] = True
        stack = [(0, len(points) - 1)]
        while stack:
            i, j = stack.pop()
            (ax, ay), (bx, by) = points[i], points[j]
            dx, dy = bx - ax, by - ay
            norm = math.hypot(dx, dy) or 1.0
            far, far_d = -1, tol
            for k in range(i + 1, j):
                px_, py_ = points[k]
                d = abs(dy * (px_ - ax) - dx * (py_ - ay)) / norm
                if d > far_d:
                    far, far_d = k, d
            if far >= 0:
                keep[far] = True
                stack += [(i, far), (far, j)]
        kept = [q for q, k in zip(points, keep, strict=True) if k]
    out: list[Pixel] = []
    for x, y in kept:
        q = (round(x), round(y))
        if not out or out[-1] != q:
            out.append(q)
    return out


def relative(points: list[Pixel]) -> str:
    """An SVG path through ``points`` with relative moves (short)."""
    (x, y), rest = points[0], points[1:]
    parts = [f"M{x} {y}"]
    for nx, ny in rest:
        parts.append(f"l{nx - x} {ny - y}")
        x, y = nx, ny
    return "".join(parts)


def chains(edges: set[tuple[Pixel, Pixel]]) -> list[list[Pixel]]:
    """Edges joined into polylines through points where exactly two meet."""
    adj: dict[Pixel, set[Pixel]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    left = {(min(a, b), max(a, b)) for a, b in edges}
    out: list[list[Pixel]] = []

    def walk(a: Pixel, b: Pixel) -> list[Pixel]:
        line = [a, b]
        left.discard((min(a, b), max(a, b)))
        while len(adj[b]) == 2:
            nxt = next(c for c in adj[b] if c != line[-2])
            key = (min(b, nxt), max(b, nxt))
            if key not in left:
                break
            left.discard(key)
            line.append(nxt)
            b = nxt
        return line

    for a in sorted(adj):
        if len(adj[a]) != 2:
            # Lazily: each walk uses up edges the next check must see.
            out.extend(walk(a, b) for b in sorted(adj[a]) if (min(a, b), max(a, b)) in left)
    while left:  # loops
        a, b = min(left)
        out.append(walk(a, b))
    return out


@dataclass(frozen=True, slots=True)
class Facts:
    area_km2: float
    stations: int
    lines: dict[str, int]
    street_km: float
    cafes: int
    jail: str


def _outline(world: World) -> list[Point] | None:
    """The district's polygon in metres, ``None`` for a circle."""
    area = world.area
    if isinstance(area, CircleArea):
        return None
    s, w, n, e = area_bbox(area)
    vertices = getattr(area, "vertices", None) or [(s, w), (s, e), (n, e), (n, w)]
    return [world.proj.to_xy(lat, lon) for lat, lon in vertices]


def _area_m2(world: World) -> float:
    area = world.area
    if isinstance(area, CircleArea):
        return math.pi * area.radius_m**2
    ring = _outline(world) or []
    return abs(
        sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1], strict=True)) / 2
    )


def district_facts(world: World) -> Facts:
    tt = world.tt
    lines: dict[str, set[str]] = {}
    for p in range(tt.n_patterns):
        lines.setdefault(tt.pattern_mode(p).value, set()).add(tt.pat_route_name[p])
    street_m = 0.0
    if world.streets is not None:
        xy = world.streets.xy
        street_m = sum(math.dist(xy[a], xy[b]) for a, b in world.streets.edges)
    jail = world.jail_spot()[1]
    return Facts(
        round(_area_m2(world) / 1e6, 1),
        tt.n_nodes,
        {mode: len(names) for mode, names in sorted(lines.items())},
        round(street_m / 1000),
        len(world.cafes),
        jail,
    )


def district_svg(world: World, size: int = 560, margin_m: float = 120.0) -> str:
    """An SVG map of the district, ``size`` pixels on its long side, north up."""
    s, w, n, e = area_bbox(world.area)
    (x0, y0), (x1, y1) = world.proj.to_xy(s, w), world.proj.to_xy(n, e)
    x0, y0, x1, y1 = x0 - margin_m, y0 - margin_m, x1 + margin_m, y1 + margin_m
    scale = size / max(x1 - x0, y1 - y0)
    width, height = round((x1 - x0) * scale), round((y1 - y0) * scale)

    def px(p: Point) -> tuple[float, float]:
        return round((p[0] - x0) * scale, 1), round((y1 - p[1]) * scale, 1)

    def path(points: list[Point]) -> str:
        cells = [px(p) for p in points]
        return "M" + "L".join(f"{a} {b}" for a, b in cells)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="#f4f1ea"/>',
    ]
    if world.water is not None:
        out.append(_water(world, px, scale))
    if world.streets is not None:
        xy = world.streets.xy

        def whole(p: Point) -> Pixel:
            a, b = px(p)
            return round(a), round(b)

        segments = {(whole(xy[a]), whole(xy[b])) for a, b in world.streets.edges}
        lines = chains({(a, b) for a, b in segments if a != b})
        d = "".join(relative(simplify([(float(x), float(y)) for x, y in c])) for c in lines)
        out.append(f'<path d="{d}" stroke="#c9c2b4" stroke-width="0.6" fill="none"/>')
    ring = _outline(world)
    outline_style = 'fill="none" stroke="#495057" stroke-width="2" stroke-dasharray="6 4"'
    if ring is None:
        assert isinstance(world.area, CircleArea)
        cx, cy = px(world.proj.to_xy(world.area.center_lat, world.area.center_lon))
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="{round(world.area.radius_m * scale, 1)}" '
            f"{outline_style}/>"
        )
    else:
        out.append(f'<path d="{path(ring)}Z" {outline_style}/>')
    tt = world.tt
    drawn: dict[tuple[str, tuple[int, ...]], tuple[Mode, str, list[Point]]] = {}
    for p in range(tt.n_patterns):
        key = (tt.pat_route_name[p], tuple(sorted(tt.pattern_stops(p))))
        line = world.pattern_path(p)
        if key not in drawn or len(line) > len(drawn[key][2]):
            drawn[key] = (tt.pattern_mode(p), tt.pat_route_name[p], line)
    for mode, width_px in LINE_STYLE.items():
        for m, route, line in drawn.values():
            if m is not mode or len(line) < 2:
                continue
            color = METRO_COLORS.get(route, MODE_COLORS[mode]) if mode is Mode.METRO else None
            out.append(
                f'<path d="{relative(simplify([px(q) for q in line]))}" '
                f'stroke="{color or MODE_COLORS[mode]}" '
                f'stroke-width="{width_px}" fill="none" stroke-linejoin="round" '
                f'stroke-linecap="round" opacity="0.9"/>'
            )
    for node in range(tt.n_nodes):
        cx, cy = px(world.node_xy[node])
        out.append(
            f'<circle cx="{cx}" cy="{cy}" r="2.4" fill="#fff" stroke="#343a40" stroke-width="1"/>'
        )
    for cafe in world.cafes:
        cx, cy = px(cafe.pos)
        out.append(f'<circle cx="{cx}" cy="{cy}" r="4" fill="#8d5524" stroke="#fff"/>')
    jail_pos, _ = world.jail_spot()
    cx, cy = px(jail_pos)
    out.append(
        f'<rect x="{cx - 4}" y="{cy - 4}" width="8" height="8" fill="#1971c2" stroke="#fff"/>'
    )
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _water(world: World, px: Callable[[Point], tuple[float, float]], scale: float) -> str:
    """Water as rows of blocks (``WATER_BLOCK_M``), one path."""
    mask = world.water
    assert mask is not None
    k = max(1, round(WATER_BLOCK_M / mask.cell))
    rows, cols = mask.water.shape
    grid = mask.water[: rows - rows % k, : cols - cols % k]
    blocks = grid.reshape(rows // k, k, cols // k, k).mean(axis=(1, 3)) >= 0.5
    step = mask.cell * k
    parts = []
    for r in range(blocks.shape[0]):
        row = np.flatnonzero(blocks[r])
        if row.size == 0:
            continue
        # Runs of water in this row.
        breaks = np.flatnonzero(np.diff(row) > 1)
        starts = np.concatenate(([row[0]], row[breaks + 1]))
        ends = np.concatenate((row[breaks], [row[-1]]))
        y_top = mask.y0 + (r + 1) * step
        for c0, c1 in zip(starts, ends, strict=True):
            x = mask.x0 + c0 * step
            left, top = px((x, y_top))
            w = round((c1 - c0 + 1) * step * scale, 1)
            h = round(step * scale, 1)
            parts.append(f"M{left} {top}h{w}v{h}h{-w}Z")
    return f'<path d="{"".join(parts)}" fill="#a5d8ff"/>'


@dataclass(frozen=True, slots=True)
class DistrictPage:
    """One district's entry in docs/districts.md."""

    index: int
    name: str
    #: The picture's path, relative to the markdown file.
    picture: str
    facts: Facts
    #: Extra Bounty Hunter bots, and whether the district sets its own.
    hunters: int
    own_hunters: bool

    @property
    def anchor(self) -> str:
        return self.picture.rsplit("/", 1)[-1].removesuffix(".svg")


def _pct(k: int, n: int) -> str:
    return f"{100 * k / n:.0f}%" if n else "-"


def _rounds(outcomes: list[Outcome], district: int, variant: str, dodger: str) -> list[Outcome]:
    return [
        o
        for o in outcomes
        if o.district == district and o.variant == variant and o.dodger == dodger
    ]


def _rate(rounds: list[Outcome]) -> str:
    return _pct(sum(o.escaped for o in rounds), len(rounds))


def districts_markdown(
    pages: list[DistrictPage],
    runs: list[tuple[str, list[Outcome]]],
    default_hunters: int,
) -> str:
    """docs/districts.md: an overview table, then each district with its picture, facts and
    balance results (from each spree in ``runs``: a title and its rounds). The overview shows
    the last spree's ``baseline``."""
    final = runs[-1][1] if runs else []
    lines = [
        "# Districts",
        "",
        "<!-- Generated by `uv run fare-dodger districts`; change configs/live.toml instead. -->",
        "",
        "The live game's districts (`configs/live.toml`), one per round. In the pictures, metro",
        "lines are in their own colours, trams yellow, trolleybuses orange, buses blue and",
        "suburban rail green. Stations are white dots, cafés brown, the jail a blue square, and",
        "the dashed line is the edge of play.",
        "",
        f"Bounty Hunters get {default_hunters} extra bots unless a district sets its own (in",
        f"bold). The bot Dodger should escape about {100 * TARGET:.0f}% of rounds, and a Dodger",
        "standing still should be caught (ADR 0019).",
        "",
        "| # | District | km² | Stations | Extra hunters | Bot Dodger escapes | Standing still |",
        "|---|---|---|---|---|---|---|",
    ]
    for page in pages:
        hunters = f"**{page.hunters}**" if page.own_hunters else str(page.hunters)
        lines.append(
            f"| {page.index} | [{page.name}](#{page.anchor}) | {page.facts.area_km2} "
            f"| {page.facts.stations} | {hunters} "
            f"| {_rate(_rounds(final, page.index, 'baseline', 'bot'))} "
            f"| {_rate(_rounds(final, page.index, 'baseline', 'still'))} |"
        )
    if runs:
        lines += ["", "Balance results: " + "; ".join(t for t, _ in runs) + "."]
    for page in pages:
        f = page.facts
        modes = ", ".join(f"{n} {mode.replace('_', ' ')}" for mode, n in f.lines.items())
        own = " (its own)" if page.own_hunters else " (the default)"
        lines += [
            "",
            f'<a id="{page.anchor}"></a>',
            "",
            f"## {page.index}. {page.name}",
            "",
            f'<img src="{page.picture}" alt="Map of {page.name}" width="420">',
            "",
            f"- {f.area_km2} km², {f.stations} stations, {f.street_km} km of streets",
            f"- Lines: {modes}",
            f"- {f.cafes} cafés; jail: {f.jail}",
            f"- Extra Bounty Hunters: {page.hunters}{own}",
        ]
        for title, outcomes in runs:
            variants = sorted(
                {o.variant for o in outcomes if o.district == page.index},
                key=lambda v: (v != "baseline", v),
            )
            if not variants:
                continue
            lines += [
                "",
                f"{title}:",
                "",
                "| Variant | Bot Dodger escapes (95% CI) | Day | Night | Standing still |",
                "|---|---|---|---|---|",
            ]
            for v in variants:
                bot = _rounds(outcomes, page.index, v, "bot")
                k = sum(o.escaped for o in bot)
                lo, hi = wilson(k, len(bot))
                lines.append(
                    f"| {v} | {_pct(k, len(bot))} ({100 * lo:.0f}-{100 * hi:.0f}%) "
                    f"| {_rate([o for o in bot if not o.night])} "
                    f"| {_rate([o for o in bot if o.night])} "
                    f"| {_rate(_rounds(outcomes, page.index, v, 'still'))} |"
                )
    return "\n".join(lines) + "\n"

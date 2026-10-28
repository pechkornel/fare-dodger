"""Coffee shops: real cafés from OpenStreetMap where the Dodger can buy a speed boost.

Cafés (``amenity=cafe`` nodes) are downloaded once from the Overpass API for a bounding box
and cached as JSON, like the streets. Each district then picks a few of them inside its play
area, spread out (farthest-point sampling from the one nearest the centre) and, when walking
is on streets, close enough to a street to be reached.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import httpx

from fare_dodger.live.streets import BBox, overpass
from fare_dodger.live.world import Point, World

#: Bump when the query changes so cached downloads are refreshed.
CAFES_VERSION = 1
#: A café farther than this from any street cannot be reached on foot (metres).
MAX_STREET_GAP_M = 30.0


@dataclass(frozen=True, slots=True)
class Cafe:
    name: str
    pos: Point


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    return f'[out:json][timeout:60];node["amenity"="cafe"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});out;'


def download_cafes(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the cafés in ``bbox`` from Overpass into ``path`` (JSON)."""
    data = overpass(overpass_query(bbox), client or httpx.Client(timeout=httpx.Timeout(120.0)))
    cafes = [
        {"name": el.get("tags", {}).get("name", "Café"), "lat": el["lat"], "lon": el["lon"]}
        for el in data["elements"]
        if el.get("type") == "node"
    ]
    payload = {"version": CAFES_VERSION, "bbox": list(bbox), "cafes": cafes}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def read_cafes(path: Path, bbox: BBox | None = None) -> list[tuple[str, float, float]] | None:
    """Cafés as ``(name, lat, lon)`` from a cached download, or ``None`` if the file is
    missing, from another query version, or does not cover ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != CAFES_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    return [(c["name"], c["lat"], c["lon"]) for c in data["cafes"]]


def pick_cafes(cafes: list[tuple[str, float, float]], world: World, k: int) -> list[Cafe]:
    """Up to ``k`` cafés inside ``world``'s area, spread out, reachable from a street.

    With streets, each café is placed at its nearest street point so it can be reached.
    """
    streets = world.streets
    candidates: list[Cafe] = []
    for name, lat, lon in cafes:
        if not world.area.contains(lat, lon):
            continue
        pos = world.proj.to_xy(lat, lon)
        if streets is not None:
            on_street = streets.point(streets.snap(pos))
            if math.dist(pos, on_street) > MAX_STREET_GAP_M:
                continue
            pos = on_street
        candidates.append(Cafe(name, pos))
    if not candidates or k <= 0:
        return []
    candidates.sort(key=lambda c: (c.pos, c.name))
    centre = (
        sum(c.pos[0] for c in candidates) / len(candidates),
        sum(c.pos[1] for c in candidates) / len(candidates),
    )
    chosen = [min(candidates, key=lambda c: math.dist(c.pos, centre))]
    while len(chosen) < min(k, len(candidates)):
        chosen.append(
            max(
                (c for c in candidates if c not in chosen),
                key=lambda c: min(math.dist(c.pos, o.pos) for o in chosen),
            )
        )
    return chosen

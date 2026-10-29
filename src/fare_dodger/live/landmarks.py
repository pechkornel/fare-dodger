"""Landmarks: statues, monuments, museums and other sights that packages are delivered to.

Named sights are downloaded once per district from the Overpass API and cached as JSON, like
the cafés. Only things a player would recognise on the street are kept: statues, busts and
sculptures, fountains, monuments and war memorials, museums, theatres, churches, castles and
city walls, historic buildings, town halls, attractions and viewpoints. Wall plaques,
Stolpersteine, tombs and galleries are left out. Each landmark is placed at its nearest street
point, so it can be reached on foot even when it is a building.

Police stations come with the same download: one of them is the district's jail (see
:func:`pick_jail`), where Dodgers who cannot pay a fine are held. They are not package
destinations.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from fare_dodger.live.streets import BBox, overpass
from fare_dodger.live.world import Point, World

#: Bump when the query or the kinds change so cached downloads are refreshed.
LANDMARKS_VERSION = 2
#: The kind of the police stations (the jail), which are not package destinations.
POLICE = "police"
#: The jail is a police station at most this far from a station (metres), so the Bounty
#: Hunters can get there by transport.
JAIL_STATION_M = 400.0
#: A landmark farther than this from any street cannot be reached on foot (metres). Larger
#: than for cafés: a museum's centre can be deep inside its building.
MAX_STREET_GAP_M = 80.0
#: Overpass client timeout (seconds). Landmarks are only downloaded in the background.
DOWNLOAD_TIMEOUT_S = 120.0

_MEMORIALS = {
    "statue": "statue",
    "bust": "statue",
    "sculpture": "statue",
    "war_memorial": "monument",
    "monument": "monument",
    "obelisk": "monument",
    "column": "monument",
}
_HISTORIC = {
    "monument": "monument",
    "castle": "castle",
    "fort": "castle",
    "city_gate": "castle",
    "citywalls": "castle",
    "church": "church",
    "monastery": "church",
    "building": "building",
}
_ARTWORKS = {
    "statue": "statue",
    "bust": "statue",
    "sculpture": "statue",
    "fountain": "fountain",
    "mural": "mural",
}
_TOURISM = {"museum": "museum", "attraction": "attraction", "viewpoint": "viewpoint"}
_AMENITY = {
    "place_of_worship": "church",
    "theatre": "theatre",
    "fountain": "fountain",
    "townhall": "building",
    "police": POLICE,
}


@dataclass(frozen=True, slots=True)
class Landmark:
    name: str
    #: statue, monument, castle, church, building, fountain, mural, museum, attraction,
    #: viewpoint, theatre, or police (the jail).
    kind: str
    pos: Point


def landmark_kind(tags: dict[str, str]) -> str | None:
    """What kind of landmark an OpenStreetMap object is, or ``None`` if it is not one."""
    historic = tags.get("historic")
    if historic == "memorial":
        return _MEMORIALS.get(tags.get("memorial", ""))
    if historic in _HISTORIC:
        return _HISTORIC[historic]
    tourism = tags.get("tourism")
    if tourism == "artwork":
        return _ARTWORKS.get(tags.get("artwork_type", ""))
    if tourism in _TOURISM:
        return _TOURISM[tourism]
    return _AMENITY.get(tags.get("amenity", ""))


def overpass_query(bbox: BBox) -> str:
    s, w, n, e = bbox
    bb = f"({s:.4f},{w:.4f},{n:.4f},{e:.4f})"
    historic = "|".join(["memorial", *_HISTORIC])
    return (
        "[out:json][timeout:60];("
        f'nwr["historic"~"^({historic})$"]["name"]{bb};'
        f'nwr["tourism"~"^(artwork|{"|".join(_TOURISM)})$"]["name"]{bb};'
        f'nwr["amenity"~"^({"|".join(_AMENITY)})$"]["name"]{bb};'
        ");out center tags;"
    )


def parse_landmarks(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The landmarks in an Overpass answer, as ``{name, kind, lat, lon}``."""
    found = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        kind = landmark_kind(tags)
        where = el if "lat" in el else el.get("center")
        if kind is None or not tags.get("name") or not where:
            continue
        found.append({"name": tags["name"], "kind": kind, "lat": where["lat"], "lon": where["lon"]})
    return found


def download_landmarks(path: Path, bbox: BBox, client: httpx.Client | None = None) -> Path:
    """Fetch the landmarks in ``bbox`` from Overpass into ``path`` (JSON)."""
    client = client or httpx.Client(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_S))
    landmarks = parse_landmarks(overpass(overpass_query(bbox), client))
    payload = {"version": LANDMARKS_VERSION, "bbox": list(bbox), "landmarks": landmarks}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def read_landmarks(
    path: Path, bbox: BBox | None = None
) -> list[tuple[str, str, float, float]] | None:
    """Landmarks as ``(name, kind, lat, lon)`` from a cached download, or ``None`` if the
    file is missing, from another query version, or does not cover ``bbox``."""
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != LANDMARKS_VERSION:
        return None
    if bbox is not None:
        s, w, n, e = data["bbox"]
        if bbox[0] < s or bbox[1] < w or bbox[2] > n or bbox[3] > e:
            return None
    return [(m["name"], m["kind"], m["lat"], m["lon"]) for m in data["landmarks"]]


def place_landmarks(landmarks: list[tuple[str, str, float, float]], world: World) -> list[Landmark]:
    """The landmarks inside ``world``'s area that can be walked to, one per name, each at its
    nearest street point (with streets), and never in the water."""
    streets, water = world.streets, world.water
    placed: dict[str, Landmark] = {}
    for name, kind, lat, lon in sorted(landmarks):
        if name in placed or not world.area.contains(lat, lon):
            continue
        pos = world.proj.to_xy(lat, lon)
        if streets is not None and len(streets):
            on_street = streets.point(streets.snap(pos))
            if math.dist(pos, on_street) > MAX_STREET_GAP_M:
                continue
            pos = on_street
        if not world.contains(pos) or (water is not None and water.blocks(pos)):
            continue
        placed[name] = Landmark(name, kind, pos)
    return list(placed.values())


def pick_jail(police: list[Landmark], world: World) -> Landmark | None:
    """The district's jail: of the police stations within ``JAIL_STATION_M`` of a station, the
    one nearest the middle of the network (``None`` without any)."""
    xy = world.node_xy
    middle = (sum(p[0] for p in xy) / len(xy), sum(p[1] for p in xy) / len(xy))

    def near_transport(m: Landmark) -> bool:
        return min(math.dist(m.pos, p) for p in xy) <= JAIL_STATION_M

    candidates = [m for m in police if near_transport(m)]
    return min(candidates, key=lambda m: (math.dist(m.pos, middle), m.name), default=None)


def install_landmarks(landmarks: list[tuple[str, str, float, float]], world: World) -> None:
    """Place ``landmarks`` in ``world``: the sights as package destinations, and a police
    station as the jail."""
    placed = place_landmarks(landmarks, world)
    world.landmarks = [m for m in placed if m.kind != POLICE]
    world.jail = pick_jail([m for m in placed if m.kind == POLICE], world)

"""Merge platforms into station nodes.

Two platforms belong to the same node if they share a ``parent_station`` or if they have the
same :func:`station_key` (name without a rail suffix) and lie within ``radius_m`` of each other
(transitively). Deep metro platforms are often
far from the surface stops of the same name (and BKK rarely sets ``parent_station``), so if
either platform is a rail platform (metro or suburban rail) or belongs to a parent station, the
wider ``station_radius_m`` applies. Names alone are not enough:
Budapest has many unrelated stops called e.g. "Ady Endre utca" kilometres apart.

A few junctions spread their same-name stops further apart than either radius. They are
listed by name in a manual override (``name_radius_m``) with a radius of their own.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from fare_dodger.geo import distance_m

#: BKK suffixes surface stops with " M" (metro), " H" (HÉV) or " M+H" when a rail station is
#: there, optionally followed by the street, e.g. "Astoria M (Dohány utca)". The rail
#: platforms themselves carry the bare name.
_RAIL_SUFFIX = re.compile(r"\s+(?:M\+H|M|H)(?:\s+\([^)]*\))?$")


def station_key(name: str) -> str:
    """Name used to decide whether platforms belong to the same station."""
    return _RAIL_SUFFIX.sub("", name.strip())


@dataclass(frozen=True, slots=True)
class Platform:
    stop_id: str
    name: str
    lat: float
    lon: float
    parent_station: str | None
    #: Served by metro or suburban rail.
    is_rail: bool = False


@dataclass(frozen=True, slots=True)
class Cluster:
    name: str
    lat: float
    lon: float
    #: Sorted platform stop IDs.
    platform_ids: tuple[str, ...]


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:  # path compression
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic: the lexicographically smaller root wins.
            lo, hi = sorted((ra, rb))
            self._parent[hi] = lo


def merge_platforms(
    platforms: list[Platform],
    radius_m: float,
    station_radius_m: float | None = None,
    name_radius_m: Mapping[str, float] | None = None,
) -> list[Cluster]:
    """Group platforms into clusters, returned sorted by (name, first platform ID).

    ``name_radius_m`` maps a :func:`station_key` to a radius that replaces the usual one for
    platforms of that name when it is larger.
    """
    overrides = name_radius_m or {}
    uf = UnionFind([p.stop_id for p in platforms])
    wide = max(radius_m, station_radius_m or 0.0)

    by_parent: dict[str, list[str]] = defaultdict(list)
    by_name: dict[str, list[Platform]] = defaultdict(list)
    for p in platforms:
        if p.parent_station:
            by_parent[p.parent_station].append(p.stop_id)
        by_name[station_key(p.name)].append(p)

    for siblings in by_parent.values():
        for other in siblings[1:]:
            uf.union(siblings[0], other)
    for key, group in by_name.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                station = a.parent_station or b.parent_station or a.is_rail or b.is_rail
                limit = max(wide if station else radius_m, overrides.get(key, 0.0))
                if distance_m(a.lat, a.lon, b.lat, b.lon) <= limit:
                    uf.union(a.stop_id, b.stop_id)

    members_of: dict[str, list[Platform]] = defaultdict(list)
    for p in platforms:
        members_of[uf.find(p.stop_id)].append(p)

    clusters = []
    for members in members_of.values():
        names = Counter(station_key(p.name) for p in members)
        name = min(names, key=lambda n: (-names[n], n))
        clusters.append(
            Cluster(
                name=name,
                lat=sum(p.lat for p in members) / len(members),
                lon=sum(p.lon for p in members) / len(members),
                platform_ids=tuple(sorted(p.stop_id for p in members)),
            )
        )
    clusters.sort(key=lambda c: (c.name, c.platform_ids[0]))
    return clusters

"""Group trips into RAPTOR route patterns.

A pattern is a sequence of nodes served by trips that never overtake each other (FIFO). RAPTOR
relies on that property to find the earliest catchable trip with a binary search, so trips of
one GTFS route with the same stop sequence are split into several patterns when one overtakes
another (e.g. an express service).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TripSequence:
    trip_id: str
    route_id: str
    nodes: tuple[int, ...]
    arrivals: tuple[int, ...]
    departures: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Pattern:
    route_id: str
    nodes: tuple[int, ...]
    #: Trips sorted by departure from the first node.
    trips: tuple[TripSequence, ...]


def collapse_repeats(
    nodes: list[int], arrivals: list[int], departures: list[int]
) -> tuple[list[int], list[int], list[int]]:
    """Merge consecutive visits to the same node (two platforms of one station)."""
    out_n: list[int] = []
    out_a: list[int] = []
    out_d: list[int] = []
    for node, arr, dep in zip(nodes, arrivals, departures, strict=True):
        if out_n and out_n[-1] == node:
            out_d[-1] = dep
        else:
            out_n.append(node)
            out_a.append(arr)
            out_d.append(dep)
    return out_n, out_a, out_d


def _never_later(a: TripSequence, b: TripSequence) -> bool:
    """True if ``a`` is at or before ``b`` at every stop (so ``b`` may follow ``a``)."""
    return all(x <= y for x, y in zip(a.arrivals, b.arrivals, strict=True)) and all(
        x <= y for x, y in zip(a.departures, b.departures, strict=True)
    )


def build_patterns(trips: list[TripSequence]) -> list[Pattern]:
    """Group trips by (route, node sequence) and split each group into FIFO chains.

    Output is deterministic: sorted by route, node sequence and first departure.
    """
    groups: dict[tuple[str, tuple[int, ...]], list[TripSequence]] = defaultdict(list)
    for trip in trips:
        if len(trip.nodes) >= 2:
            groups[(trip.route_id, trip.nodes)].append(trip)

    patterns: list[Pattern] = []
    for (route_id, nodes), members in sorted(groups.items()):
        members.sort(key=lambda t: (t.departures[0], t.arrivals[-1], t.trip_id))
        chains: list[list[TripSequence]] = []
        for trip in members:
            for chain in chains:
                if _never_later(chain[-1], trip):
                    chain.append(trip)
                    break
            else:
                chains.append([trip])
        patterns.extend(Pattern(route_id, nodes, tuple(chain)) for chain in chains)
    return patterns

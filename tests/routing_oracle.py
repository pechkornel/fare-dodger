"""Brute-force reference router used to test RAPTOR.

Dijkstra over (node, arrival kind) states with exactly the semantics documented in
``fare_dodger.routing.raptor``: change time after a vehicle arrival, no walk-to-walk chains,
no limit on the number of vehicles.
"""

from __future__ import annotations

import heapq

from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, RideLeg, WalkLeg

SOURCE, RIDE, WALK = 0, 1, 2


def earliest_arrivals(
    tt: Timetable, sources: list[int], t_dep: int, max_time: int, mode_mask: int
) -> dict[int, int]:
    heap = [(t_dep, s, SOURCE) for s in sorted(set(sources))]
    done: set[tuple[int, int]] = set()
    best: dict[int, int] = {}
    while heap:
        t, node, kind = heapq.heappop(heap)
        if (node, kind) in done or t > max_time:
            continue
        done.add((node, kind))
        best[node] = min(best.get(node, t), t)
        if kind in (SOURCE, RIDE):
            for to, dur in tt.footpaths(node):
                heapq.heappush(heap, (t + dur, to, WALK))
        ready = t + tt.node_change_s[node] if kind == RIDE else t
        for k in range(tt.node_pat_ptr[node], tt.node_pat_ptr[node + 1]):
            pattern, pos = tt.node_pat[k], tt.node_pat_pos[k]
            if not (mode_mask >> tt.pat_mode[pattern]) & 1:
                continue
            stops = tt.pattern_stops(pattern)
            for trip in range(tt.pat_trip_ptr[pattern], tt.pat_trip_ptr[pattern + 1]):
                off = tt.trip_time_ptr[trip]
                if tt.trip_dep[off + pos] < ready:
                    continue
                for j in range(pos + 1, len(stops)):
                    arr = tt.trip_arr[off + j]
                    if arr <= max_time:
                        heapq.heappush(heap, (arr, stops[j], RIDE))
    return best


def assert_consistent(tt: Timetable, journey: Journey, sources: set[int], max_time: int) -> None:
    """Structural checks every RAPTOR journey must satisfy."""
    assert journey.origin in sources
    clock = journey.depart
    position = journey.origin
    prev_kind = "source"
    for leg in journey.legs:
        assert leg.from_node == position
        if isinstance(leg, WalkLeg):
            assert prev_kind != "walk", "walk-to-walk chains are not allowed"
            duration = tt.walk_duration(leg.from_node, leg.to_node)
            assert duration is not None
            assert leg.dep >= clock
            assert leg.arr == leg.dep + duration
            prev_kind = "walk"
        else:
            assert isinstance(leg, RideLeg)
            stops = tt.pattern_stops(leg.pattern)
            assert stops[leg.board_pos] == leg.from_node
            assert stops[leg.alight_pos] == leg.to_node
            assert leg.board_pos < leg.alight_pos
            assert tt.pattern_of_trip(leg.trip) == leg.pattern
            off = tt.trip_time_ptr[leg.trip]
            assert tt.trip_dep[off + leg.board_pos] == leg.dep
            assert tt.trip_arr[off + leg.alight_pos] == leg.arr
            ready = clock + (tt.node_change_s[position] if prev_kind == "ride" else 0)
            assert leg.dep >= ready
            prev_kind = "ride"
        clock = leg.arr
        position = leg.to_node
    assert journey.arrival <= max_time

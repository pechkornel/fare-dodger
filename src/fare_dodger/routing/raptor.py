"""RAPTOR: Round-bAsed Public Transit Optimized Router (Delling, Pajor, Werneck 2012).

Round ``k`` finds the earliest arrival at every node using at most ``k`` vehicles. Each round
scans only the route patterns that serve a node improved in the previous round, starting at
the earliest such position. On each pattern it rides the earliest catchable trip and relaxes
the arrival at every later stop. Footpaths are then relaxed from the nodes reached by vehicle.

Semantics (shared with the rules engine's validator):

* Boarding at a source, or after arriving on foot, is possible immediately. After arriving by
  vehicle, a node's change time must pass before boarding another vehicle there.
* At most one walk between two rides (no walk-to-walk chains); an initial walk is allowed.
* Labels are kept per arrival kind (by vehicle / on foot). A vehicle arrival enables walking
  onwards and a foot arrival enables an immediate boarding, so neither dominates the other.
  With that, the result is exact for the earliest arrival at every node within ``max_time``.
* On equal arrival times fewer vehicles win, because a later round only stores strict
  improvements.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from fare_dodger.modes import ALL_VEHICLES_MASK, VEHICLE_MODES
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, Leg, RideLeg, WalkLeg

INF = 1 << 30
RIDE = 0
WALK = 1

#: ride label: (arrival, trip, pattern, board_pos, alight_pos, board_node, prev_round, prev_kind)
RideLabel = tuple[int, int, int, int, int, int, int, int]
#: walk label: (arrival, from_node); from_node == -1 marks a source
WalkLabel = tuple[int, int]


@dataclass(eq=False)
class RaptorResult:
    tt: Timetable
    sources: tuple[int, ...]
    t_dep: int
    max_time: int
    rides: list[dict[int, RideLabel]]
    walks: list[dict[int, WalkLabel]]
    best_ride: list[int]
    best_walk: list[int]

    def arrival(self, node: int) -> int | None:
        best = min(self.best_ride[node], self.best_walk[node])
        return None if best >= INF else best

    def reached(self) -> list[int]:
        """Reached nodes, sorted by arrival time then node index."""
        nodes = [n for n in range(len(self.best_ride)) if self.arrival(n) is not None]
        return sorted(nodes, key=lambda n: (min(self.best_ride[n], self.best_walk[n]), n))

    def arrivals(self) -> dict[int, int]:
        return {n: t for n in range(len(self.best_ride)) if (t := self.arrival(n)) is not None}

    def journey(self, node: int) -> Journey:
        """Reconstruct the earliest-arrival journey to ``node`` (fewest vehicles on ties)."""
        if self.arrival(node) is None:
            msg = f"node {node} is not reachable"
            raise ValueError(msg)
        tt = self.tt
        if self.best_ride[node] <= self.best_walk[node]:
            kind = RIDE
            k = max(r for r, labels in enumerate(self.rides) if node in labels)
        else:
            kind = WALK
            k = max(r for r, labels in enumerate(self.walks) if node in labels)

        legs: list[Leg] = []
        current = node
        while True:
            if kind == WALK:
                arr, origin = self.walks[k][current]
                if origin == -1:
                    break
                dep = self.t_dep if k == 0 else self.rides[k][origin][0]
                legs.append(WalkLeg(origin, current, dep, arr))
                current = origin
                kind = WALK if k == 0 else RIDE
            else:
                arr, trip, pattern, b_pos, a_pos, b_node, prev_round, prev_kind = self.rides[k][
                    current
                ]
                off = tt.trip_time_ptr[trip]
                legs.append(
                    RideLeg(
                        trip=trip,
                        pattern=pattern,
                        board_pos=b_pos,
                        alight_pos=a_pos,
                        from_node=b_node,
                        to_node=current,
                        dep=tt.trip_dep[off + b_pos],
                        arr=arr,
                        mode=VEHICLE_MODES[tt.pat_mode[pattern]],
                        route_name=tt.pat_route_name[pattern],
                    )
                )
                current, k, kind = b_node, prev_round, prev_kind
        legs.reverse()
        return Journey(origin=current, depart=self.t_dep, legs=tuple(legs))


def raptor(
    tt: Timetable,
    sources: Iterable[int],
    t_dep: int,
    max_time: int,
    *,
    mode_mask: int = ALL_VEHICLES_MASK,
    max_rounds: int = 8,
) -> RaptorResult:
    """Earliest arrivals from any of ``sources`` departing at ``t_dep``, up to ``max_time``."""
    # Local aliases: attribute lookups dominate the cost of tight Python loops.
    n_nodes = tt.n_nodes
    pat_mode, pat_stop_ptr, pat_stops = tt.pat_mode, tt.pat_stop_ptr, tt.pat_stops
    pat_trip_ptr, trip_time_ptr = tt.pat_trip_ptr, tt.trip_time_ptr
    trip_arr, trip_dep = tt.trip_arr, tt.trip_dep
    node_pat_ptr, node_pat, node_pat_pos = tt.node_pat_ptr, tt.node_pat, tt.node_pat_pos
    fp_ptr, fp_to, fp_dur = tt.fp_ptr, tt.fp_to, tt.fp_dur
    change = tt.node_change_s

    best_ride = [INF] * n_nodes
    best_walk = [INF] * n_nodes
    ready = [INF] * n_nodes
    ready_src: list[tuple[int, int]] = [(-1, -1)] * n_nodes
    rides: list[dict[int, RideLabel]] = [{}]
    walks: list[dict[int, WalkLabel]] = [{}]
    source_tuple = tuple(sorted(set(sources)))
    marked: set[int] = set()

    if t_dep <= max_time:
        for s in source_tuple:
            walks[0][s] = (t_dep, -1)
            best_walk[s] = ready[s] = t_dep
            ready_src[s] = (0, WALK)
            marked.add(s)
        for s in source_tuple:
            for k in range(fp_ptr[s], fp_ptr[s + 1]):
                m, t = fp_to[k], t_dep + fp_dur[k]
                if t <= max_time and t < best_walk[m]:
                    best_walk[m] = t
                    walks[0][m] = (t, s)
                    if t < ready[m]:
                        ready[m] = t
                        ready_src[m] = (0, WALK)
                        marked.add(m)

    for rnd in range(1, max_rounds + 1):
        if not marked:
            break
        ready_prev = ready[:]
        src_prev = ready_src[:]
        queue: dict[int, int] = {}
        for n in marked:
            for idx in range(node_pat_ptr[n], node_pat_ptr[n + 1]):
                p = node_pat[idx]
                if not (mode_mask >> pat_mode[p]) & 1:
                    continue
                pos = node_pat_pos[idx]
                if pos < queue.get(p, INF):
                    queue[p] = pos

        ride_labels: dict[int, RideLabel] = {}
        new_marked: set[int] = set()
        for p in sorted(queue):
            s0 = pat_stop_ptr[p]
            length = pat_stop_ptr[p + 1] - s0
            t_lo, t_hi = pat_trip_ptr[p], pat_trip_ptr[p + 1]
            trip = -1
            off = 0
            board_pos = board_node = 0
            board_src = (-1, -1)
            for i in range(queue[p], length):
                node = pat_stops[s0 + i]
                if trip >= 0:
                    a = trip_arr[off + i]
                    # No early exit when a > max_time: an earlier trip may still be caught
                    # at a later stop that was reached by other means.
                    if a <= max_time and a < best_ride[node]:
                        best_ride[node] = a
                        ride_labels[node] = (
                            a, trip, p, board_pos, i, board_node, board_src[0], board_src[1],
                        )  # fmt: skip
                        r = a + change[node]
                        if r < ready[node]:
                            ready[node] = r
                            ready_src[node] = (rnd, RIDE)
                            new_marked.add(node)
                rp = ready_prev[node]
                if rp >= INF or i == length - 1 or (trip >= 0 and rp > trip_dep[off + i]):
                    continue
                # Earliest trip departing at position i no earlier than rp (FIFO => sorted).
                lo, hi = t_lo, (trip + 1 if trip >= 0 else t_hi)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if trip_dep[trip_time_ptr[mid] + i] < rp:
                        lo = mid + 1
                    else:
                        hi = mid
                if lo < (trip if trip >= 0 else t_hi):
                    dep = trip_dep[trip_time_ptr[lo] + i]
                    if dep <= max_time:
                        trip = lo
                        off = trip_time_ptr[lo]
                        board_pos, board_node, board_src = i, node, src_prev[node]

        walk_labels: dict[int, WalkLabel] = {}
        for n in sorted(ride_labels):
            a = ride_labels[n][0]
            for k in range(fp_ptr[n], fp_ptr[n + 1]):
                m, t = fp_to[k], a + fp_dur[k]
                if t <= max_time and t < best_walk[m]:
                    best_walk[m] = t
                    walk_labels[m] = (t, n)
                    if t < ready[m]:
                        ready[m] = t
                        ready_src[m] = (rnd, WALK)
                        new_marked.add(m)
        rides.append(ride_labels)
        walks.append(walk_labels)
        marked = new_marked

    return RaptorResult(
        tt=tt,
        sources=source_tuple,
        t_dep=t_dep,
        max_time=max_time,
        rides=rides,
        walks=walks,
        best_ride=best_ride,
        best_walk=best_walk,
    )


def reachable(
    tt: Timetable,
    sources: Iterable[int],
    t_dep: int,
    horizon_s: int,
    *,
    mode_mask: int = ALL_VEHICLES_MASK,
    max_rounds: int = 8,
) -> RaptorResult:
    """Every node reachable from ``sources`` leaving at ``t_dep`` within ``horizon_s`` seconds."""
    return raptor(tt, sources, t_dep, t_dep + horizon_s, mode_mask=mode_mask, max_rounds=max_rounds)

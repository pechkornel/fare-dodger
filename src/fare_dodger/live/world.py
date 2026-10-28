"""The timetable seen in continuous time: map coordinates, vehicle positions, departures.

Positions are metres on a local flat projection (x east, y north) around the network's
centre. Vehicles move in straight lines between consecutive stops at constant speed and
stand at a stop from its arrival to its departure time.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass

from fare_dodger.config import Area
from fare_dodger.geo import metres_per_degree
from fare_dodger.modes import Mode
from fare_dodger.network.timetable import Timetable

Point = tuple[float, float]


def distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def lerp(a: Point, b: Point, f: float) -> Point:
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f


def step_towards(a: Point, b: Point, max_step: float) -> Point:
    """Move from ``a`` towards ``b`` by at most ``max_step`` metres."""
    d = distance(a, b)
    return b if d <= max_step else lerp(a, b, max_step / d)


@dataclass(frozen=True, slots=True)
class Projection:
    lat0: float
    lon0: float

    def to_xy(self, lat: float, lon: float) -> Point:
        m_lat, m_lon = metres_per_degree(self.lat0)
        return (lon - self.lon0) * m_lon, (lat - self.lat0) * m_lat

    def to_latlon(self, p: Point) -> tuple[float, float]:
        m_lat, m_lon = metres_per_degree(self.lat0)
        return self.lat0 + p[1] / m_lat, self.lon0 + p[0] / m_lon


@dataclass(frozen=True, slots=True)
class TripState:
    trip: int
    pos: Point
    #: Pattern index of the last stop the vehicle has arrived at.
    stop: int
    #: Standing at a stop (or within a few seconds of one), so doors are open.
    at_stop: bool


@dataclass(frozen=True, slots=True)
class Departure:
    trip: int
    pattern: int
    #: Index of the boarding stop in the pattern.
    pos: int
    node: int
    time: int
    route: str
    mode: Mode
    headsign: str


class World:
    #: A vehicle this many seconds from a stop still counts as standing there.
    AT_STOP_GRACE_S = 5

    def __init__(self, tt: Timetable, area: Area) -> None:
        self.tt = tt
        self.area = area
        n = tt.n_nodes
        self.proj = Projection(sum(tt.node_lat) / n, sum(tt.node_lon) / n)
        self.node_xy = [self.proj.to_xy(tt.node_lat[i], tt.node_lon[i]) for i in range(n)]
        self.trip_pattern = [0] * tt.n_trips
        for p in range(tt.n_patterns):
            for trip in range(tt.pat_trip_ptr[p], tt.pat_trip_ptr[p + 1]):
                self.trip_pattern[trip] = p
        self._stops = [tt.pattern_stops(p) for p in range(tt.n_patterns)]
        self._times = [self._trip_times(trip) for trip in range(tt.n_trips)]
        #: Stations where at least one pattern continues (trips clipped at the area edge
        #: only end at some stations).
        self.boarding_nodes = [
            node
            for node in range(n)
            if any(
                tt.node_pat_pos[idx] < len(self._stops[tt.node_pat[idx]]) - 1
                for idx in range(tt.node_pat_ptr[node], tt.node_pat_ptr[node + 1])
            )
        ]

    def _trip_times(self, trip: int) -> tuple[list[int], list[int]]:
        length = len(self._stops[self.trip_pattern[trip]])
        off = self.tt.trip_time_ptr[trip]
        return self.tt.trip_arr[off : off + length], self.tt.trip_dep[off : off + length]

    # --- geometry --------------------------------------------------------------------------

    def contains(self, p: Point) -> bool:
        return self.area.contains(*self.proj.to_latlon(p))

    def nearest_node(self, p: Point, max_dist: float = math.inf) -> int | None:
        best, best_d = None, max_dist
        for i, q in enumerate(self.node_xy):
            d = distance(p, q)
            if d <= best_d:
                best, best_d = i, d
        return best

    # --- vehicles --------------------------------------------------------------------------

    def stops(self, trip: int) -> list[int]:
        return self._stops[self.trip_pattern[trip]]

    def times(self, trip: int) -> tuple[list[int], list[int]]:
        return self._times[trip]

    def route(self, trip: int) -> str:
        return self.tt.pat_route_name[self.trip_pattern[trip]]

    def mode(self, trip: int) -> Mode:
        return self.tt.pattern_mode(self.trip_pattern[trip])

    def headsign(self, pattern: int) -> str:
        return self.tt.node_name[self._stops[pattern][-1]]

    def trip_state(self, trip: int, t: float) -> TripState | None:
        """Where ``trip`` is at time ``t``, or ``None`` before its first or after its last stop."""
        arr, dep = self._times[trip]
        if t < arr[0] or t > arr[-1]:
            return None
        stops = self.stops(trip)
        i = bisect_right(arr, t) - 1
        if t <= dep[i] or i == len(arr) - 1:
            return TripState(trip, self.node_xy[stops[i]], i, at_stop=True)
        f = (t - dep[i]) / (arr[i + 1] - dep[i])
        pos = lerp(self.node_xy[stops[i]], self.node_xy[stops[i + 1]], f)
        grace = self.AT_STOP_GRACE_S
        at_stop = t - dep[i] <= grace or arr[i + 1] - t <= grace
        return TripState(trip, pos, i, at_stop)

    def active_trips(self, t: float) -> list[TripState]:
        states = []
        for trip, (arr, _) in enumerate(self._times):
            if arr[0] <= t <= arr[-1]:
                state = self.trip_state(trip, t)
                if state is not None:
                    states.append(state)
        return states

    def departures(self, node: int, t: float, horizon_s: float, limit: int) -> list[Departure]:
        """The next vehicles leaving ``node`` (not ending there) within ``horizon_s``."""
        tt = self.tt
        found = []
        for idx in range(tt.node_pat_ptr[node], tt.node_pat_ptr[node + 1]):
            p, pos = tt.node_pat[idx], tt.node_pat_pos[idx]
            if pos == len(self._stops[p]) - 1:
                continue
            for trip in range(tt.pat_trip_ptr[p], tt.pat_trip_ptr[p + 1]):
                time = self._times[trip][1][pos]
                if t <= time <= t + horizon_s:
                    found.append(
                        Departure(
                            trip,
                            p,
                            pos,
                            node,
                            time,
                            tt.pat_route_name[p],
                            tt.pattern_mode(p),
                            self.headsign(p),
                        )
                    )
        found.sort(key=lambda d: (d.time, d.route, d.trip))
        return found[:limit]

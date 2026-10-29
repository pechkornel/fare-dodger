"""The timetable seen in continuous time: map coordinates, vehicle positions, departures.

Positions are metres on a local flat projection (x east, y north) around the network's
centre. Vehicles move between consecutive stops at constant speed, along the hop's GTFS shape
when one is given (see :mod:`fare_dodger.live.shapes`) and in a straight line otherwise, and
stand at a stop from its arrival to its departure time.

Vehicles stop where their route passes the platform (the shape's point for the stop), not at
the merged station's centre, which can be 150 m away: going through the centre made every
line zig-zag at its stops. Travellers board and get off at those stop points.
"""

from __future__ import annotations

import itertools
import math
import random
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fare_dodger.config import Area
from fare_dodger.geo import metres_per_degree
from fare_dodger.live.shapes import Hops, smooth_path
from fare_dodger.live.streets import RouteFollower, StreetGraph, along
from fare_dodger.modes import Mode
from fare_dodger.network.timetable import Timetable

if TYPE_CHECKING:
    from fare_dodger.live.buildings import Buildings
    from fare_dodger.live.cafes import Cafe
    from fare_dodger.live.landmarks import Landmark
    from fare_dodger.live.water import WaterMask

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
    #: Direction of travel (radians, counter-clockwise from east).
    heading: float = 0.0


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
    #: Vehicles are shown driving in this long before their first stop and away this long
    #: after their last (timetable seconds), growing in and shrinking out, instead of
    #: popping up and vanishing at a stop.
    SHOW_LEAD_S = 20
    #: Speed of that drive (m/s, about 30 km/h).
    SHOW_SPEED_MPS = 8.0

    def __init__(self, tt: Timetable, area: Area, hops: Hops | None = None) -> None:
        self.tt = tt
        self.area = area
        n = tt.n_nodes
        self.proj = Projection(sum(tt.node_lat) / n, sum(tt.node_lon) / n)
        self.node_xy = [self.proj.to_xy(tt.node_lat[i], tt.node_lon[i]) for i in range(n)]
        #: Walkable streets (see :meth:`set_streets`); ``None`` means free walking.
        self.streets: StreetGraph | None = None
        #: Where each station meets the street (the station itself without streets).
        self.stop_xy = list(self.node_xy)
        #: Coffee shops (see :mod:`fare_dodger.live.cafes`).
        self.cafes: list[Cafe] = []
        #: Sights that packages are delivered to (see :mod:`fare_dodger.live.landmarks`).
        self.landmarks: list[Landmark] = []
        #: The police station where Dodgers who cannot pay a fine are held (see
        #: :meth:`jail_spot` for districts without one).
        self.jail: Landmark | None = None
        #: Footprints that block walking (see :mod:`fare_dodger.live.buildings`).
        self.buildings: Buildings | None = None
        #: Water that blocks walking, bridges excepted (see :mod:`fare_dodger.live.water`).
        self.water: WaterMask | None = None
        self.trip_pattern = [0] * tt.n_trips
        for p in range(tt.n_patterns):
            for trip in range(tt.pat_trip_ptr[p], tt.pat_trip_ptr[p + 1]):
                self.trip_pattern[trip] = p
        self._stops = [tt.pattern_stops(p) for p in range(tt.n_patterns)]
        #: Per pattern, per stop: where its vehicles stand.
        self._stop_points: list[list[Point]] = []
        #: Per pattern, per hop: the path from one stop to the next and its cumulative lengths.
        self._paths: list[list[tuple[list[Point], list[float]]]] = []
        #: Per station: every point where a vehicle stops there.
        self.platform_xy: list[list[Point]] = [[] for _ in range(n)]
        for p, stops in enumerate(self._stops):
            shaped = [
                smooth_path([self.proj.to_xy(lat, lon) for lat, lon in hops[p][i]])
                if hops and hops[p][i]
                else []
                for i in range(len(stops) - 1)
            ]
            points = []
            for i, node in enumerate(stops):
                if i > 0 and shaped[i - 1]:
                    points.append(shaped[i - 1][-1])  # where it arrives
                elif i < len(shaped) and shaped[i]:
                    points.append(shaped[i][0])
                else:
                    points.append(self.node_xy[node])
                if points[-1] not in self.platform_xy[node]:
                    self.platform_xy[node].append(points[-1])
            pattern_paths = []
            for i in range(len(stops) - 1):
                path = shaped[i] or [points[i + 1]]
                if distance(points[i], path[0]) > 1e-6:
                    path = [points[i], *path]  # leaves from where it arrived
                cum = [0.0]
                for a, b in itertools.pairwise(path):
                    cum.append(cum[-1] + distance(a, b))
                pattern_paths.append((path, cum))
            self._stop_points.append(points)
            self._paths.append(pattern_paths)
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

    def set_streets(self, streets: StreetGraph | None) -> None:
        self.streets = streets
        self.stop_xy = (
            [streets.point(streets.snap(p)) for p in self.node_xy]
            if streets is not None
            else list(self.node_xy)
        )

    def random_spot(
        self, rng: random.Random, away_from_stops_m: float = 60.0, tries: int = 60
    ) -> Point | None:
        """A random walkable point: on a street (near a station without street data), inside
        the area, outside every building and away from the stops."""
        for _ in range(tries):
            if self.streets is not None and len(self.streets):
                p = rng.choice(self.streets.xy)
            else:
                bx, by = rng.choice(self.node_xy)
                angle, r = rng.uniform(0, 2 * math.pi), rng.uniform(80.0, 250.0)
                p = (bx + r * math.cos(angle), by + r * math.sin(angle))
            if not self.contains(p):
                continue
            if self.buildings is not None and self.buildings.at(p) is not None:
                continue
            if self.water is not None and self.water.blocks(p):
                continue
            if any(distance(p, q) < away_from_stops_m for q in self.stop_xy):
                continue
            return p
        return None

    def jail_spot(self) -> tuple[Point, str]:
        """Where the jail is, and its name: the district's police station, or else (no
        police data yet) by the station nearest the middle of the network."""
        if self.jail is not None:
            return self.jail.pos, self.jail.name
        xy = self.node_xy
        middle = (sum(p[0] for p in xy) / len(xy), sum(p[1] for p in xy) / len(xy))
        node = min(range(len(xy)), key=lambda n: (distance(xy[n], middle), n))
        return self.stop_xy[node], f"the police post at {self.tt.node_name[node]}"

    def stop_distance(self, p: Point, node: int) -> float:
        """How far ``p`` is from station ``node``: its centre, where it meets the street, or
        where a vehicle stops there, whichever is closest."""
        return min(
            distance(p, self.node_xy[node]),
            distance(p, self.stop_xy[node]),
            *(distance(p, q) for q in self.platform_xy[node]),
        )

    def stop_point(self, pattern: int, pos: int) -> Point:
        """Where vehicles of ``pattern`` stand at stop ``pos``."""
        return self._stop_points[pattern][pos]

    def nearest_stop(self, p: Point, max_dist: float) -> int | None:
        best, best_d = None, max_dist
        for node in range(len(self.node_xy)):
            d = self.stop_distance(p, node)
            if d <= best_d:
                best, best_d = node, d
        return best

    def walk_towards(
        self, follower: RouteFollower, pos: Point, target: Point, max_step: float
    ) -> Point:
        """Walk up to ``max_step`` metres towards ``target``: along the streets, if any."""
        if self.streets is None:
            return step_towards(pos, target, max_step)
        return follower.step(self.streets, pos, target, max_step)

    def walk_point(self, a: Point, b: Point, f: float) -> Point:
        """A fraction ``f`` of the way on foot from ``a`` to ``b`` (along the streets, if any)."""
        if self.streets is None:
            return lerp(a, b, f)
        return along(self.streets.station_path(a, b, (a, b)), f)

    def contains(self, p: Point) -> bool:
        return self.area.contains(*self.proj.to_latlon(p))

    def hop_point(self, pattern: int, hop: int, f: float) -> Point:
        """The point a fraction ``f`` of the way along ``hop`` of ``pattern``."""
        path, cum = self._paths[pattern][hop]
        if len(path) == 2:
            return lerp(path[0], path[1], f)
        target = f * cum[-1]
        j = min(max(bisect_right(cum, target) - 1, 0), len(path) - 2)
        span = cum[j + 1] - cum[j]
        return lerp(path[j], path[j + 1], (target - cum[j]) / span if span > 0 else 0.0)

    def hop_heading(self, pattern: int, hop: int, f: float) -> float:
        """Direction of travel a fraction ``f`` of the way along ``hop`` of ``pattern``."""
        path, cum = self._paths[pattern][hop]
        target = f * cum[-1]
        j = min(max(bisect_right(cum, target) - 1, 0), len(path) - 2)
        while j < len(path) - 2 and cum[j + 1] - cum[j] < 1e-6:
            j += 1
        (x0, y0), (x1, y1) = path[j], path[j + 1]
        return math.atan2(y1 - y0, x1 - x0)

    def pattern_path(self, pattern: int) -> list[Point]:
        """The whole path of ``pattern``, stop to stop."""
        hops = self._paths[pattern]
        return [hops[0][0][0], *(q for path, _ in hops for q in path[1:])] if hops else []

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
        i = bisect_right(arr, t) - 1
        pattern = self.trip_pattern[trip]
        if t <= dep[i] or i == len(arr) - 1:
            # Standing: facing the way it will leave (or the way it came, at the last stop).
            last = i == len(arr) - 1
            heading = (
                self.hop_heading(pattern, i - 1, 1.0) if last else self.hop_heading(pattern, i, 0.0)
            )
            return TripState(trip, self._stop_points[pattern][i], i, True, heading)
        f = (t - dep[i]) / (arr[i + 1] - dep[i])
        pos = self.hop_point(pattern, i, f)
        grace = self.AT_STOP_GRACE_S
        at_stop = t - dep[i] <= grace or arr[i + 1] - t <= grace
        return TripState(trip, pos, i, at_stop, self.hop_heading(pattern, i, f))

    def active_trips(self, t: float) -> list[TripState]:
        states = []
        for trip, (arr, _) in enumerate(self._times):
            if arr[0] <= t <= arr[-1]:
                state = self.trip_state(trip, t)
                if state is not None:
                    states.append(state)
        return states

    def shown_trips(self, t: float) -> list[tuple[TripState, float]]:
        """The vehicles to draw at time ``t``, each with how much of it shows (0 to 1): the
        active trips in full, and the ones about to start or just finished driving in from or
        away along their direction of travel. Only for drawing: nobody can board those."""
        lead = self.SHOW_LEAD_S
        shown: list[tuple[TripState, float]] = []
        for trip, (arr, _) in enumerate(self._times):
            if t < arr[0] - lead or t > arr[-1] + lead:
                continue
            pattern = self.trip_pattern[trip]
            if t < arr[0]:
                ahead, pos = arr[0] - t, self._stop_points[pattern][0]
                heading = self.hop_heading(pattern, 0, 0.0) if len(arr) > 1 else 0.0
                back = -ahead * self.SHOW_SPEED_MPS
                x, y = pos[0] + back * math.cos(heading), pos[1] + back * math.sin(heading)
                shown.append((TripState(trip, (x, y), 0, False, heading), 1.0 - ahead / lead))
            elif t > arr[-1]:
                gone, last = t - arr[-1], len(arr) - 1
                pos = self._stop_points[pattern][last]
                heading = self.hop_heading(pattern, last - 1, 1.0) if last > 0 else 0.0
                on = gone * self.SHOW_SPEED_MPS
                x, y = pos[0] + on * math.cos(heading), pos[1] + on * math.sin(heading)
                shown.append((TripState(trip, (x, y), last, False, heading), 1.0 - gone / lead))
            else:
                active = self.trip_state(trip, t)
                if active is not None:
                    shown.append((active, 1.0))
        return shown

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

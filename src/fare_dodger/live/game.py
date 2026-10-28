"""One live game: a human Dodger against bot Bounty Hunters in continuous timetable time.

The Dodger walks freely inside the play area and rides real trips. To board, the Dodger
stands at a stop and waits for a departure (the next one, or one picked from the list), and
is aboard when the vehicle leaves. Asking to get off makes the Dodger alight at the next stop
(or the current one, if the vehicle is standing there).

Fares: the Dodger can buy a single ticket at any stop, which is used on the next boarding,
or ride without one. Ticket inspectors ride a hidden share of the trips. Caught without a
ticket, the Dodger is put off at the next stop and held there while the fine is processed
(and reported to the Bounty Hunters), then pays; a Dodger who cannot pay loses.

The Dodger is caught for good when a Bounty Hunter rides the same trip, or when a Bounty
Hunter on foot gets within ``catch_radius_m`` while the Dodger is exposed: on foot (or held),
or on a vehicle that is standing at a stop. The Bounty Hunters learn the Dodger's position
every ``ping_every_s``; the Dodger always sees the Bounty Hunters.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.bots import BountyHunters, Ping
from fare_dodger.live.world import Departure, Point, World, distance
from fare_dodger.routing.raptor import reachable
from fare_dodger.timeutil import format_hms

#: Departures listed at a stop: how far ahead and how many.
DEPARTURE_HORIZON_S = 15 * 60
DEPARTURE_LIMIT = 6
#: Movement and catches are evaluated at least this often (timetable seconds).
MAX_SUBSTEP_S = 1.0


class Status(StrEnum):
    RUNNING = "running"
    CAUGHT = "caught"
    ESCAPED = "escaped"


@dataclass
class Keys:
    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False

    def direction(self) -> Point:
        dx = float(self.right) - float(self.left)
        dy = float(self.up) - float(self.down)
        norm = (dx * dx + dy * dy) ** 0.5
        return (dx / norm, dy / norm) if norm else (0.0, 0.0)


@dataclass
class Ride:
    trip: int
    board_pos: int
    #: Pattern index of the stop to get off at, once requested.
    alight_pos: int | None = None
    #: A ticket was used when boarding.
    ticket: bool = False
    #: Caught without a ticket: put off at ``alight_pos`` to pay a fine.
    fined: bool = False


@dataclass
class Dodger:
    pos: Point
    ride: Ride | None = None
    waiting_for: Departure | None = None
    walked_m: float = 0.0
    rides: int = 0
    money: int = 0
    tickets: int = 0
    tickets_bought: int = 0
    fines: int = 0
    #: Held at a stop until this time while a fine is processed.
    held_until: float | None = None


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    text: str


@dataclass
class LiveGame:
    world: World
    cfg: LiveConfig
    seed: int
    t: float = field(init=False)
    status: Status = field(init=False, default=Status.RUNNING)
    keys: Keys = field(init=False, default_factory=Keys)
    pings: list[Ping] = field(init=False, default_factory=list)
    caught_by: str | None = field(init=False, default=None)
    _events: list[Event] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.t = float(self.cfg.start_seconds)
        self.next_ping = self.cfg.start_seconds + self.cfg.ping_every_s
        rng = random.Random(self.seed)
        dodger_node, hunter_nodes = self._starts(rng)
        self.dodger = Dodger(self.world.node_xy[dodger_node], money=self.cfg.start_money_ft)
        # A separate stream, so the inspected trips do not change the start positions.
        inspected = random.Random(f"{self.seed}:inspected")
        self.inspected = frozenset(
            trip
            for trip in range(self.world.tt.n_trips)
            if inspected.random() < self.cfg.inspected_share
        )
        self.hunters = BountyHunters(self.world, self.cfg, hunter_nodes, rng)
        self._emit("start", f"You start at {self.world.tt.node_name[dodger_node]}. Stay hidden!")

    def _starts(self, rng: random.Random) -> tuple[int, list[int]]:
        """Random starts at stations with onward departures (not dead ends at the area edge),
        every Bounty Hunter at least ``min_start_distance_min`` away when possible."""
        world, k = self.world, self.cfg.n_hunters
        nodes = world.boarding_nodes or list(range(world.tt.n_nodes))
        dodger = rng.choice(nodes)
        near = set(
            reachable(
                world.tt, [dodger], self.cfg.start_seconds, self.cfg.min_start_distance_min * 60
            ).reached()
        )
        far = [n for n in nodes if n not in near]
        if len(far) >= k:
            return dodger, rng.sample(far, k)
        others = sorted(
            (n for n in nodes if n != dodger),
            key=lambda n: (-distance(world.node_xy[n], world.node_xy[dodger]), n),
        ) or [dodger]
        return dodger, [others[i % len(others)] for i in range(k)]

    def _emit(self, kind: str, text: str) -> None:
        self._events.append(Event(kind, text))

    def drain_events(self) -> list[Event]:
        events, self._events = self._events, []
        return events

    # --- input -----------------------------------------------------------------------------

    def current_stop(self) -> int | None:
        if self.dodger.ride is not None or self.dodger.held_until is not None:
            return None
        return self.world.nearest_node(self.dodger.pos, self.cfg.board_radius_m)

    def departures(self) -> list[Departure]:
        stop = self.current_stop()
        if stop is None:
            return []
        return self.world.departures(stop, self.t, DEPARTURE_HORIZON_S, DEPARTURE_LIMIT)

    def buy_ticket(self) -> None:
        """Buy a single ticket at the stop's machine."""
        if self.status is not Status.RUNNING:
            return
        d, price = self.dodger, self.cfg.ticket_price_ft
        if self.current_stop() is None:
            self._emit("info", "Ticket machines are at the stops.")
        elif d.money < price:
            self._emit("info", f"Not enough money: a ticket costs {price} Ft.")
        else:
            d.money -= price
            d.tickets += 1
            d.tickets_bought += 1
            self._emit("ticket", f"Ticket bought ({price} Ft). It is used on your next ride.")

    def use(self) -> None:
        """The action key: get off, stop waiting, or wait for the next departure."""
        if self.status is not Status.RUNNING:
            return
        d = self.dodger
        if d.held_until is not None:
            return
        if d.ride is not None:
            if d.ride.fined:
                self._emit("info", "The inspectors are taking you off at the next stop.")
                return
            self._request_alight(d.ride)
        elif d.waiting_for is not None:
            d.waiting_for = None
            self._emit("info", "You stopped waiting.")
        else:
            self.wait_for(0)

    def wait_for(self, index: int) -> None:
        """Wait at the current stop for departure ``index`` of the listed departures."""
        d = self.dodger
        if self.status is not Status.RUNNING or d.ride is not None or d.held_until is not None:
            return
        departures = self.departures()
        if self.current_stop() is None:
            self._emit("info", "Walk to a stop first.")
        elif index >= len(departures):
            self._emit("info", "No such departure.")
        else:
            dep = departures[index]
            self.dodger.waiting_for = dep
            self._emit("info", f"Waiting for {dep.route} towards {dep.headsign}.")

    def _request_alight(self, ride: Ride) -> None:
        state = self.world.trip_state(ride.trip, self.t)
        if state is None:
            return
        arr, dep = self.world.times(ride.trip)
        last = len(arr) - 1
        here = state.stop if self.t <= dep[state.stop] else state.stop + 1
        ride.alight_pos = min(max(here, ride.board_pos + 1), last)
        name = self.world.tt.node_name[self.world.stops(ride.trip)[ride.alight_pos]]
        self._emit("info", f"Getting off at {name}.")

    # --- simulation ------------------------------------------------------------------------

    def step(self, dt: float) -> None:
        """Advance the game by ``dt`` timetable seconds."""
        while dt > 0 and self.status is Status.RUNNING:
            h = min(dt, MAX_SUBSTEP_S)
            self._advance(h)
            dt -= h

    def _advance(self, h: float) -> None:
        self.t = min(self.t + h, self.cfg.end_seconds)
        self._move_dodger(h)
        d = self.dodger
        self.hunters.advance(self.t, h, d.pos, d.ride is None)
        if self.t >= self.next_ping:
            self._ping()
        self._check_catch()
        if self.status is Status.RUNNING and self.t >= self.cfg.end_seconds:
            self.status = Status.ESCAPED
            self._emit("end", "You made it! The Bounty Hunters gave up.")

    def _move_dodger(self, h: float) -> None:
        d, world = self.dodger, self.world
        if d.held_until is not None:
            if self.t >= d.held_until:
                self._pay_fine(d)
            return
        if d.ride is not None:
            self._ride(d, d.ride)
            return
        if d.waiting_for is not None:
            dep = d.waiting_for
            if distance(d.pos, world.node_xy[dep.node]) > self.cfg.board_radius_m:
                d.waiting_for = None
                self._emit("info", "You left the stop.")
            elif self.t >= dep.time:
                d.waiting_for = None
                d.ride = Ride(dep.trip, dep.pos)
                d.pos = world.node_xy[dep.node]
                d.rides += 1
                self._emit("board", f"You boarded {dep.route} towards {dep.headsign}.")
                self._board(d, d.ride, dep)
                return
        dx, dy = self.keys.direction()
        if dx == 0 and dy == 0:
            return
        step = self.cfg.walk_speed_mps * h
        x, y = d.pos
        for candidate in ((x + dx * step, y + dy * step), (x + dx * step, y), (x, y + dy * step)):
            if candidate != d.pos and world.contains(candidate):
                d.walked_m += distance(d.pos, candidate)
                d.pos = candidate
                return

    def _board(self, d: Dodger, ride: Ride, dep: Departure) -> None:
        if d.tickets > 0:
            d.tickets -= 1
            ride.ticket = True
        if dep.trip not in self.inspected:
            return
        if ride.ticket:
            self._emit("inspection", "Ticket inspection! Your ticket is valid. Phew.")
            return
        if d.money < self.cfg.fine_ft:
            self._caught(f"Ticket inspection! You can't pay the {self.cfg.fine_ft} Ft fine.")
            return
        ride.fined = True
        ride.alight_pos = dep.pos + 1
        name = self.world.tt.node_name[self.world.stops(ride.trip)[ride.alight_pos]]
        self._emit("inspection", f"Ticket inspection! No ticket: you're off at {name}.")

    def _pay_fine(self, d: Dodger) -> None:
        d.held_until = None
        d.money -= self.cfg.fine_ft
        d.fines += 1
        self._emit("fine", f"You paid a {self.cfg.fine_ft} Ft fine and are free to go.")

    def _ride(self, d: Dodger, ride: Ride) -> None:
        world = self.world
        arr, _ = world.times(ride.trip)
        last = len(arr) - 1
        target = ride.alight_pos
        if target is None and self.t >= arr[last]:
            target = last
        if target is not None and self.t >= arr[target]:
            node = world.stops(ride.trip)[target]
            d.pos, d.ride = world.node_xy[node], None
            if ride.fined:
                d.held_until = self.t + self.cfg.fine_hold_s
                self._emit("fine", f"Held at {world.tt.node_name[node]} while the fine is written.")
                if self.cfg.fine_alerts_hunters:
                    self._report(node, "The inspectors reported you")
            else:
                self._emit("alight", f"You got off at {world.tt.node_name[node]}.")
            return
        state = world.trip_state(ride.trip, self.t)
        if state is not None:
            d.pos = state.pos

    def _ping(self) -> None:
        node = self.world.nearest_node(self.dodger.pos)
        assert node is not None
        self.next_ping += self.cfg.ping_every_s
        self._report(node, "Spotted")

    def _report(self, node: int, text: str) -> None:
        """Tell the Bounty Hunters where the Dodger is."""
        ping = Ping(int(self.t), self.dodger.pos, node)
        self.pings.append(ping)
        self.hunters.on_ping(ping)
        self._emit("ping", f"{text} near {self.world.tt.node_name[node]}!")

    def _check_catch(self) -> None:
        d = self.dodger
        trip = d.ride.trip if d.ride else None
        exposed = True
        if trip is not None:
            state = self.world.trip_state(trip, self.t)
            exposed = state is not None and state.at_stop
        for agent in self.hunters.states():
            if trip is not None and agent.trip == trip:
                self._caught(f"A Bounty Hunter was riding the {self.world.route(trip)} with you.")
                return
            if (
                agent.trip is None
                and exposed
                and distance(agent.pos, d.pos) <= self.cfg.catch_radius_m
            ):
                self._caught("A Bounty Hunter caught you.")
                return

    def _caught(self, text: str) -> None:
        self.status = Status.CAUGHT
        self.caught_by = text
        self._emit("end", text)

    # --- client view -----------------------------------------------------------------------

    def _latlon(self, p: Point) -> list[float]:
        lat, lon = self.world.proj.to_latlon(p)
        return [round(lat, 6), round(lon, 6)]

    def snapshot(self) -> dict[str, Any]:
        world, d, t = self.world, self.dodger, self.t
        names = world.tt.node_name
        ride = None
        if d.ride is not None:
            r = d.ride
            stops = world.stops(r.trip)
            state = world.trip_state(r.trip, t)
            nxt = min((state.stop + 1) if state else len(stops) - 1, len(stops) - 1)
            ride = {
                "trip": r.trip,
                "route": world.route(r.trip),
                "mode": world.mode(r.trip).value,
                "headsign": names[stops[-1]],
                "next_stop": names[stops[nxt]],
                "alight_at": names[stops[r.alight_pos]] if r.alight_pos is not None else None,
                "ticket": r.ticket,
                "fined": r.fined,
            }
        stop = self.current_stop()
        waiting = d.waiting_for
        stop_view = None
        if stop is not None:
            stop_view = {
                "name": names[stop],
                "departures": [
                    {
                        "route": dep.route,
                        "mode": dep.mode.value,
                        "headsign": dep.headsign,
                        "in_s": round(dep.time - t),
                        "waiting": waiting is not None and waiting.trip == dep.trip,
                    }
                    for dep in self.departures()
                ],
            }
        last_ping = self.pings[-1] if self.pings else None
        return {
            "t": round(t, 1),
            "clock": format_hms(int(t)),
            "remaining_s": round(self.cfg.end_seconds - t),
            "next_ping_s": round(self.next_ping - t),
            "status": self.status.value,
            "message": self.caught_by,
            "player": {
                "pos": self._latlon(d.pos),
                "state": (
                    "ride"
                    if d.ride
                    else "held"
                    if d.held_until is not None
                    else "wait"
                    if waiting
                    else "walk"
                ),
            },
            "wallet": {"money": d.money, "tickets": d.tickets},
            "held_s": round(d.held_until - t) if d.held_until is not None else None,
            "waiting": (
                {
                    "route": waiting.route,
                    "headsign": waiting.headsign,
                    "in_s": round(waiting.time - t),
                }
                if waiting
                else None
            ),
            "ride": ride,
            "stop": stop_view,
            "hunters": [
                {"pos": self._latlon(a.pos), "riding": a.trip is not None, "chasing": ag.chasing}
                for a, ag in zip(self.hunters.states(), self.hunters.agents, strict=True)
            ],
            "vehicles": [
                [s.trip, *self._latlon(s.pos), world.mode(s.trip).value, world.route(s.trip)]
                for s in world.active_trips(t)
            ],
            "ping": (
                {"pos": self._latlon(last_ping.pos), "age_s": round(t - last_ping.time)}
                if last_ping
                else None
            ),
            "nearest_hunter_m": round(
                min((distance(a.pos, d.pos) for a in self.hunters.agents), default=0.0)
            ),
            "stats": {
                "walked_m": round(d.walked_m),
                "rides": d.rides,
                "tickets_bought": d.tickets_bought,
                "fines": d.fines,
            },
            "events": [{"kind": e.kind, "text": e.text} for e in self.drain_events()],
        }


def world_view(world: World, cfg: LiveConfig) -> dict[str, Any]:
    """Static map data for the client: area, stations and route lines."""
    tt = world.tt
    lines: dict[tuple[str, tuple[int, ...]], dict[str, Any]] = {}
    for p in range(tt.n_patterns):
        stops = tuple(tt.pattern_stops(p))
        # Both directions of a line share one entry: keep the more detailed path (a direction
        # without a GTFS shape is drawn stop to stop).
        key = (tt.pat_route_name[p], tuple(sorted(stops)))
        path = [[round(c, 6) for c in world.proj.to_latlon(q)] for q in world.pattern_path(p)]
        if key not in lines or len(path) > len(lines[key]["path"]):
            lines[key] = {
                "route": tt.pat_route_name[p],
                "mode": tt.pattern_mode(p).value,
                "path": path,
            }
    return {
        "center": [world.proj.lat0, world.proj.lon0],
        "area": world.area.model_dump(),
        "stations": [
            {"name": tt.node_name[n], "pos": [tt.node_lat[n], tt.node_lon[n]]}
            for n in range(tt.n_nodes)
        ],
        "lines": list(lines.values()),
        "tiles": {"url": cfg.tile_url, "attribution": cfg.tile_attribution},
        "time_scale": cfg.time_scale,
        "ping_every_s": cfg.ping_every_s,
        "sight_radius_m": cfg.sight_radius_m,
        "catch_radius_m": cfg.catch_radius_m,
        "board_radius_m": cfg.board_radius_m,
        "ticket_price_ft": cfg.ticket_price_ft,
        "fine_ft": cfg.fine_ft,
    }

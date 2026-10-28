"""One live round: the Dodger against the Bounty Hunters in continuous timetable time.

The human plays either side (``role``). As the Dodger, bot Bounty Hunters give chase; as a
Bounty Hunter, the human hunts a bot Dodger together with bot teammates. Everyone moves with
the same mechanics (:class:`Traveller`): walk freely inside the play area, stand at a stop and
wait for a departure (the next one, or one picked from the list), be aboard when it leaves,
and ask to get off at the next stop (or the current one, if the vehicle is standing there).

Fares (the Dodger only; Bounty Hunters ride free): a single ticket bought at any stop is used
on the next boarding. Ticket inspectors ride a hidden share of the trips. Caught without a
ticket, the Dodger is put off at the next stop and held there while the fine is processed
(and reported to the Bounty Hunters), then pays; a Dodger who cannot pay loses.

The Bounty Hunters win when one of them rides the same trip as the Dodger, or when both are
on foot (waiting at a stop and being held count as on foot) within ``catch_radius_m``. They
learn the Dodger's position every ``ping_every_s`` and see a Dodger who is not riding within
``sight_radius_m``. The Dodger always sees the Bounty Hunters
and wins by lasting ``duration_minutes``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.bots import AgentState, BountyHunters, Ping
from fare_dodger.live.dodger_bot import BotDodger
from fare_dodger.live.streets import EdgePos, RouteFollower
from fare_dodger.live.world import Departure, Point, World, distance
from fare_dodger.routing.raptor import reachable
from fare_dodger.timeutil import format_hms

#: Departures listed at a stop: how far ahead and how many.
DEPARTURE_HORIZON_S = 15 * 60
DEPARTURE_LIMIT = 6
#: Movement and catches are evaluated at least this often (timetable seconds).
MAX_SUBSTEP_S = 1.0
#: Leaving a park, a street this close is stepped onto (metres).
PARK_EXIT_M = 15.0
#: Open ground this far ahead (across a pavement) can be walked onto (metres).
PARK_ENTER_M = 12.0


class Status(StrEnum):
    RUNNING = "running"
    CAUGHT = "caught"
    ESCAPED = "escaped"


class Role(StrEnum):
    DODGER = "dodger"
    HUNTER = "hunter"

    @property
    def other(self) -> Role:
        return Role.HUNTER if self is Role.DODGER else Role.DODGER


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
class Traveller:
    """Someone walking and riding: the Dodger, or the human playing a Bounty Hunter."""

    pos: Point
    #: Pays fares and can be fined (the Dodger).
    pays_fares: bool = True
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
    #: Standing at a café until this time while a coffee is made.
    brewing_until: float | None = None
    #: Running on coffee until this time.
    boosted_until: float | None = None
    coffees: int = 0
    #: Where on the street graph, when walking on streets (``None``: snap on the next step).
    street: EdgePos | None = None
    follower: RouteFollower = field(default_factory=RouteFollower)


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    text: str


@dataclass
class LiveGame:
    world: World
    cfg: LiveConfig
    seed: int
    role: Role = Role.DODGER
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
        xy, names = self.world.node_xy, self.world.tt.node_name
        self.dodger = Traveller(xy[dodger_node], money=self.cfg.start_money_ft)
        # A separate stream, so the inspected trips do not change the start positions.
        inspected = random.Random(f"{self.seed}:inspected")
        self.inspected = frozenset(
            trip
            for trip in range(self.world.tt.n_trips)
            if inspected.random() < self.cfg.inspected_share
        )
        self.dodger_bot: BotDodger | None = None
        if self.role is Role.DODGER:
            self.player = self.dodger
            self.hunters = BountyHunters(self.world, self.cfg, hunter_nodes, rng)
            self._emit("start", f"You start at {names[dodger_node]}. Stay hidden!")
        else:
            self.player = Traveller(xy[hunter_nodes[0]], pays_fares=False)
            self.hunters = BountyHunters(self.world, self.cfg, hunter_nodes[1:], rng)
            self.dodger_bot = BotDodger(self.world, self.cfg, random.Random(f"{self.seed}:bot"))
            self._emit(
                "start",
                f"You are a Bounty Hunter at {names[hunter_nodes[0]]}. Find the Dodger!",
            )

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

    @property
    def as_dodger(self) -> bool:
        return self.role is Role.DODGER

    def _emit(self, kind: str, text: str) -> None:
        self._events.append(Event(kind, text))

    def _tell(self, who: Traveller, kind: str, text: str) -> None:
        """An event about ``who``, shown only when it is the human player."""
        if who is self.player:
            self._emit(kind, text)

    def drain_events(self) -> list[Event]:
        events, self._events = self._events, []
        return events

    # --- input (the human player) ----------------------------------------------------------

    def stop_of(self, who: Traveller) -> int | None:
        if who.ride is not None or who.held_until is not None or who.brewing_until is not None:
            return None
        return self.world.nearest_stop(who.pos, self.cfg.board_radius_m)

    def current_stop(self) -> int | None:
        return self.stop_of(self.player)

    def departures(self) -> list[Departure]:
        stop = self.current_stop()
        if stop is None:
            return []
        return self.world.departures(stop, self.t, DEPARTURE_HORIZON_S, DEPARTURE_LIMIT)

    def cafe_at(self, who: Traveller) -> int | None:
        """The café ``who`` is standing at (on foot), if any."""
        if who.ride is not None or who.held_until is not None:
            return None
        best, best_d = None, self.cfg.cafe_radius_m
        for i, cafe in enumerate(self.world.cafes):
            d = distance(who.pos, cafe.pos)
            if d <= best_d:
                best, best_d = i, d
        return best

    def buy_coffee(self) -> None:
        """Buy a coffee at the café: stand still while it is made, then run fast for a while."""
        p, cfg = self.player, self.cfg
        if self.status is not Status.RUNNING or p.brewing_until is not None:
            return
        if not p.pays_fares:
            self._emit("info", "Bounty Hunters are on duty: no coffee breaks.")
            return
        cafe = self.cafe_at(p)
        if cafe is None:
            self._emit("info", "Find a café first (☕ on the map).")
        elif p.money < cfg.coffee_price_ft:
            self._emit("info", f"Not enough money: a coffee costs {cfg.coffee_price_ft} Ft.")
        else:
            p.money -= cfg.coffee_price_ft
            p.waiting_for = None
            p.brewing_until = self.t + cfg.coffee_brew_s
            self._emit(
                "coffee",
                f"One coffee at {self.world.cafes[cafe].name} ({cfg.coffee_price_ft} Ft), "
                "coming up…",
            )

    def buy_ticket(self) -> None:
        """Buy a single ticket at the stop's machine."""
        if self.status is not Status.RUNNING:
            return
        if not self.player.pays_fares:
            self._emit("info", "Bounty Hunters ride for free.")
            return
        self.buy_ticket_for(self.player)

    def buy_ticket_for(self, who: Traveller) -> bool:
        price = self.cfg.ticket_price_ft
        if self.stop_of(who) is None:
            self._tell(who, "info", "Ticket machines are at the stops.")
        elif who.money < price:
            self._tell(who, "info", f"Not enough money: a ticket costs {price} Ft.")
        else:
            who.money -= price
            who.tickets += 1
            who.tickets_bought += 1
            self._tell(who, "ticket", f"Ticket bought ({price} Ft). It is used on your next ride.")
            return True
        return False

    def use(self) -> None:
        """The action key: get off, stop waiting, or wait for the next departure."""
        if self.status is not Status.RUNNING:
            return
        p = self.player
        if p.held_until is not None or p.brewing_until is not None:
            return
        if p.ride is not None:
            if p.ride.fined:
                self._emit("info", "The inspectors are taking you off at the next stop.")
                return
            if p.ride.alight_pos is not None:  # changed your mind: stay on
                p.ride.alight_pos = None
                self._emit("info", "You stay on.")
                return
            self._request_alight(p.ride)
        elif p.waiting_for is not None:
            p.waiting_for = None
            self._emit("info", "You stopped waiting.")
        else:
            self.wait_for(0)

    def wait_for(self, index: int) -> None:
        """Wait at the current stop for departure ``index`` of the listed departures."""
        p = self.player
        if self.status is not Status.RUNNING or p.ride is not None or p.held_until is not None:
            return
        if p.brewing_until is not None:
            self._emit("info", "Your coffee is nearly ready…")
            return
        departures = self.departures()
        if self.current_stop() is None:
            self._emit("info", "Walk to a stop first.")
        elif index >= len(departures):
            self._emit("info", "No such departure.")
        else:
            dep = departures[index]
            p.waiting_for = dep
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
        d = self.dodger
        if self.as_dodger:
            self._move(d, h, self.keys.direction(), None)
        else:
            target, direction = (
                self.dodger_bot.control(self) if self.dodger_bot is not None else (None, None)
            )
            self._move(d, h, direction, target)
            self._move(self.player, h, self.keys.direction(), None)
        if self.status is not Status.RUNNING:
            return
        self.hunters.advance(self.t, h, d.pos, d.ride is None)
        if self.t >= self.next_ping:
            self._ping()
        self._check_catch()
        if self.status is Status.RUNNING and self.t >= self.cfg.end_seconds:
            self.status = Status.ESCAPED
            self._emit(
                "end",
                "You made it! The Bounty Hunters gave up."
                if self.as_dodger
                else "Time's up: the Dodger got away.",
            )

    def _move(
        self, who: Traveller, h: float, direction: Point | None, target: Point | None
    ) -> None:
        """Advance ``who`` by ``h`` seconds: walking along ``direction`` (a unit vector) or
        straight towards ``target``, or waiting, boarding, riding and paying fines."""
        world = self.world
        if who.boosted_until is not None and self.t >= who.boosted_until:
            who.boosted_until = None
            self._tell(who, "coffee", "The coffee wore off.")
        if who.held_until is not None:
            if self.t >= who.held_until:
                self._pay_fine(who)
            return
        if who.brewing_until is not None:
            if self.t >= who.brewing_until:
                who.brewing_until = None
                who.boosted_until = self.t + self.cfg.coffee_boost_s
                who.coffees += 1
                self._tell(who, "coffee", "☕ Coffee! You're running fast for a while.")
            return
        if who.ride is not None:
            self._ride(who, who.ride)
            return
        if who.waiting_for is not None:
            dep = who.waiting_for
            if world.stop_distance(who.pos, dep.node) > self.cfg.board_radius_m:
                who.waiting_for = None
                self._tell(who, "info", "You left the stop.")
            elif self.t >= dep.time:
                who.waiting_for = None
                who.ride = Ride(dep.trip, dep.pos)
                who.pos = world.stop_point(dep.pattern, dep.pos)
                who.street = None
                who.follower.reset()
                who.rides += 1
                self._tell(who, "board", f"You boarded {dep.route} towards {dep.headsign}.")
                if who.pays_fares:
                    self._board(who, who.ride, dep)
                return
        boosted = who.boosted_until is not None
        step = (self.cfg.coffee_speed_mps if boosted else self.cfg.walk_speed_mps) * h
        if world.streets is not None:
            self._walk_streets(who, step, direction, target)
            return
        if target is not None:
            gap = distance(who.pos, target)
            if gap < 1e-9:
                return
            step = min(step, gap)
            direction = ((target[0] - who.pos[0]) / gap, (target[1] - who.pos[1]) / gap)
        if direction is None or direction == (0.0, 0.0):
            return
        dx, dy = direction
        x, y = who.pos
        for candidate in ((x + dx * step, y + dy * step), (x + dx * step, y), (x, y + dy * step)):
            if candidate != who.pos and world.contains(candidate):
                who.walked_m += distance(who.pos, candidate)
                who.pos = candidate
                return

    def _walk_streets(
        self, who: Traveller, step: float, direction: Point | None, target: Point | None
    ) -> None:
        streets = self.world.streets
        assert streets is not None
        old = who.pos
        if target is not None:
            who.pos = streets_pos = self.world.walk_towards(who.follower, who.pos, target, step)
            who.street = None
        else:
            if direction is None or direction == (0.0, 0.0):
                return
            if self._walk_open_ground(who, step, direction):
                who.walked_m += distance(old, who.pos)
                return
            here = who.street
            if here is None or distance(streets.point(here), who.pos) > 1e-6:
                here = streets.snap(who.pos)  # e.g. after getting off: step onto the street
            who.street = streets.move(here, direction, step)
            who.pos = streets_pos = streets.point(who.street)
        who.walked_m += distance(old, streets_pos)

    def _walk_open_ground(self, who: Traveller, step: float, direction: Point) -> bool:
        """Walk freely across parks and squares. Returns ``False`` to walk on the streets
        instead: when the way ahead is not open ground and a street is close by."""
        parks, streets, world = self.world.parks, self.world.streets, self.world
        if parks is None or streets is None:
            return False
        x, y = who.pos
        dx, dy = direction

        def open_ground(p: Point) -> bool:
            return parks.contains(p) and world.contains(p)

        straight = (x + dx * step, y + dy * step)
        # Parks usually start a few metres off the street: head for one just ahead too.
        probe = (x + dx * PARK_ENTER_M, y + dy * PARK_ENTER_M)
        if open_ground(straight) or (open_ground(probe) and world.contains(straight)):
            who.pos, who.street = straight, None
            return True
        if who.street is not None or not parks.contains(who.pos):
            return False  # on a street: keep to the streets
        # In a park, facing its edge: step onto a street close by, or slide along the edge.
        if distance(streets.point(streets.snap(who.pos)), who.pos) <= PARK_EXIT_M:
            return False
        for slide in ((x + dx * step, y), (x, y + dy * step)):
            if slide != who.pos and open_ground(slide):
                who.pos = slide
                return True
        return True  # stuck at the edge, with no street nearby

    def _board(self, d: Traveller, ride: Ride, dep: Departure) -> None:
        if d.tickets > 0:
            d.tickets -= 1
            ride.ticket = True
        if dep.trip not in self.inspected:
            return
        if ride.ticket:
            self._tell(d, "inspection", "Ticket inspection! Your ticket is valid. Phew.")
            return
        if d.money < self.cfg.fine_ft:
            self._caught(
                f"Ticket inspection! You can't pay the {self.cfg.fine_ft} Ft fine."
                if self.as_dodger
                else "Ticket inspectors caught the Dodger, who couldn't pay the fine."
            )
            return
        ride.fined = True
        ride.alight_pos = dep.pos + 1
        name = self.world.tt.node_name[self.world.stops(ride.trip)[ride.alight_pos]]
        self._tell(d, "inspection", f"Ticket inspection! No ticket: you're off at {name}.")

    def _pay_fine(self, d: Traveller) -> None:
        d.held_until = None
        d.money -= self.cfg.fine_ft
        d.fines += 1
        self._tell(d, "fine", f"You paid a {self.cfg.fine_ft} Ft fine and are free to go.")

    def _ride(self, who: Traveller, ride: Ride) -> None:
        world = self.world
        arr, _ = world.times(ride.trip)
        last = len(arr) - 1
        target = ride.alight_pos
        if target is None and self.t >= arr[last]:
            target = last
        if target is not None and self.t >= arr[target]:
            node = world.stops(ride.trip)[target]
            who.pos = world.stop_point(world.trip_pattern[ride.trip], target)
            who.ride = None
            name = world.tt.node_name[node]
            if ride.fined:
                who.held_until = self.t + self.cfg.fine_hold_s
                self._tell(who, "fine", f"Held at {name} while the fine is written.")
                if self.cfg.fine_alerts_hunters:
                    self._report(node, "The inspectors reported you", "Inspectors fined the Dodger")
            else:
                self._tell(who, "alight", f"You got off at {name}.")
            return
        state = world.trip_state(ride.trip, self.t)
        if state is not None:
            who.pos = state.pos

    def _ping(self) -> None:
        node = self.world.nearest_node(self.dodger.pos)
        assert node is not None
        self.next_ping += self.cfg.ping_every_s
        self._report(node, "Spotted", "The Dodger was spotted")

    def _report(self, node: int, as_dodger: str, as_hunter: str) -> None:
        """Tell the Bounty Hunters where the Dodger is."""
        ping = Ping(int(self.t), self.dodger.pos, node)
        self.pings.append(ping)
        self.hunters.on_ping(ping)
        text = as_dodger if self.as_dodger else as_hunter
        self._emit("ping", f"{text} near {self.world.tt.node_name[node]}!")

    def hunter_states(self) -> list[tuple[AgentState, bool]]:
        """Every Bounty Hunter's state, and whether it is the human player."""
        states = [(s, False) for s in self.hunters.states()]
        if not self.as_dodger:
            p = self.player
            trip = p.ride.trip if p.ride else None
            states.insert(0, (AgentState(p.pos, trip, None), True))
        return states

    def dodger_visible(self) -> bool:
        """The Bounty Hunters can see the Dodger (not riding, and one of them is close)."""
        d = self.dodger
        if d.ride is not None:
            return False
        return any(
            distance(s.pos, d.pos) <= self.cfg.sight_radius_m for s, _ in self.hunter_states()
        )

    def _check_catch(self) -> None:
        """A Bounty Hunter catches the Dodger on the same trip, or when both are on foot."""
        d = self.dodger
        trip = d.ride.trip if d.ride else None
        for agent, human in self.hunter_states():
            same_trip = trip is not None and agent.trip == trip
            on_foot_close = (
                trip is None
                and agent.trip is None
                and distance(agent.pos, d.pos) <= self.cfg.catch_radius_m
            )
            if not (same_trip or on_foot_close):
                continue
            where = f" on the {self.world.route(trip)}" if same_trip and trip is not None else ""
            if self.as_dodger:
                text = (
                    f"A Bounty Hunter was riding the {self.world.route(trip)} with you."
                    if same_trip and trip is not None
                    else "A Bounty Hunter caught you."
                )
            elif human:
                text = f"You caught the Dodger{where}!"
            else:
                text = f"Your teammate caught the Dodger{where}!"
            self._caught(text)
            return

    def _caught(self, text: str) -> None:
        self.status = Status.CAUGHT
        self.caught_by = text
        self._emit("end", text)

    @property
    def player_won(self) -> bool | None:
        """``None`` while running; otherwise whether the human's side won the round."""
        if self.status is Status.RUNNING:
            return None
        return (self.status is Status.ESCAPED) == self.as_dodger

    # --- client view -----------------------------------------------------------------------

    def _latlon(self, p: Point) -> list[float]:
        lat, lon = self.world.proj.to_latlon(p)
        return [round(lat, 6), round(lon, 6)]

    def snapshot(self) -> dict[str, Any]:
        world, p, d, t = self.world, self.player, self.dodger, self.t
        names = world.tt.node_name
        ride = None
        if p.ride is not None:
            r = p.ride
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
        waiting = p.waiting_for
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
        if p.ride:
            state_name = "ride"
        elif p.held_until is not None:
            state_name = "held"
        else:
            state_name = "wait" if waiting else "walk"
        last_ping = self.pings[-1] if self.pings else None
        visible = self.dodger_visible()
        over = self.status is not Status.RUNNING
        return {
            "t": round(t, 1),
            "clock": format_hms(int(t)),
            "remaining_s": round(self.cfg.end_seconds - t),
            "next_ping_s": round(self.next_ping - t),
            "status": self.status.value,
            "role": self.role.value,
            "won": self.player_won,
            "message": self.caught_by,
            "player": {"pos": self._latlon(p.pos), "state": state_name},
            "wallet": {"money": p.money, "tickets": p.tickets} if p.pays_fares else None,
            "held_s": round(p.held_until - t) if p.held_until is not None else None,
            "coffee": {
                "cafe": (
                    self.world.cafes[cafe_here].name
                    if (cafe_here := self.cafe_at(p)) is not None and p.pays_fares
                    else None
                ),
                "brewing_s": round(p.brewing_until - t) if p.brewing_until is not None else None,
                "boost_s": round(p.boosted_until - t) if p.boosted_until is not None else None,
            },
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
                {"pos": self._latlon(s.pos), "riding": s.trip is not None, "chasing": ag.chasing}
                for s, ag in zip(self.hunters.states(), self.hunters.agents, strict=True)
            ],
            "dodger": (
                {"pos": self._latlon(d.pos), "held": d.held_until is not None}
                if not self.as_dodger and (visible or over)
                else None
            ),
            "vehicles": [
                [
                    s.trip,
                    *self._latlon(s.pos),
                    world.mode(s.trip).value,
                    world.route(s.trip),
                    round(s.heading, 3),
                ]
                for s in world.active_trips(t)
            ],
            "ping": (
                {"pos": self._latlon(last_ping.pos), "age_s": round(t - last_ping.time)}
                if last_ping
                else None
            ),
            "nearest_hunter_m": (
                round(min((distance(s.pos, d.pos) for s, _ in self.hunter_states()), default=0.0))
                if self.as_dodger
                else None
            ),
            "seen": visible,
            "stats": {
                "walked_m": round(p.walked_m),
                "rides": p.rides,
                "tickets_bought": d.tickets_bought,
                "fines": d.fines,
                "dodger_money": d.money,
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
        "tiles": {"url": cfg.tile_url, "glyphs": cfg.glyphs_url},
        "time_scale": cfg.time_scale,
        "ping_every_s": cfg.ping_every_s,
        "sight_radius_m": cfg.sight_radius_m,
        "catch_radius_m": cfg.catch_radius_m,
        "board_radius_m": cfg.board_radius_m,
        "ticket_price_ft": cfg.ticket_price_ft,
        "fine_ft": cfg.fine_ft,
        "cafes": [
            {"name": c.name, "pos": [round(v, 6) for v in world.proj.to_latlon(c.pos)]}
            for c in world.cafes
        ],
        "coffee_price_ft": cfg.coffee_price_ft,
        "coffee_boost_s": cfg.coffee_boost_s,
    }

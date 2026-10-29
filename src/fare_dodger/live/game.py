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

import math
import random
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.bots import AgentState, BountyHunters, Ping
from fare_dodger.live.dodger_bot import BotDodger
from fare_dodger.live.streets import RouteFollower
from fare_dodger.live.world import Departure, Point, World, distance, step_towards
from fare_dodger.routing.raptor import reachable
from fare_dodger.timeutil import format_hms

#: Departures listed at a stop: how far ahead and how many.
DEPARTURE_HORIZON_S = 15 * 60
DEPARTURE_LIMIT = 6
#: Movement and catches are evaluated at least this often (timetable seconds).
MAX_SUBSTEP_S = 1.0
#: Blocked by a wall, try these turns (radians) to slide along it, the straight way first.
SLIDE_ANGLES = (0.0, 0.6, -0.6, 1.2, -1.2)
#: A package's destination is a station reachable from it with at most this many rides,
#: within this long (timetable seconds), and the landmark is this close to that station.
PACKAGE_MAX_RIDES = 2
PACKAGE_HORIZON_S = 20 * 60
PACKAGE_STATION_M = 500.0
#: Street spots tried as destinations in a district without landmarks.
PACKAGE_STREET_SPOTS = 30


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
    """Keys held, relative to the screen: up is the top of the player's map view."""

    up: bool = False
    down: bool = False
    left: bool = False
    right: bool = False
    #: The map view's rotation (degrees clockwise from north), so up means "up on screen".
    bearing: float = 0.0

    def direction(self) -> Point:
        """The unit vector to walk along (x east, y north), or ``(0, 0)``."""
        dx = float(self.right) - float(self.left)
        dy = float(self.up) - float(self.down)
        norm = (dx * dx + dy * dy) ** 0.5
        if not norm:
            return (0.0, 0.0)
        dx, dy = dx / norm, dy / norm
        b = math.radians(self.bearing)
        return (dx * math.cos(b) + dy * math.sin(b), -dx * math.sin(b) + dy * math.cos(b))


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
    #: A bot's street route.
    follower: RouteFollower = field(default_factory=RouteFollower)


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class Seat:
    """A place in a round: a human player (their id) or a bot (``player`` is ``None``)."""

    player: str | None
    name: str


@dataclass
class Package:
    """A parcel waiting to be picked up, or carried to ``dest`` by a Dodger. The destination
    is known from the start, so it shows before the package is taken."""

    pos: Point
    reward: int
    dest: Point
    #: The station nearest the destination, to name it.
    near: str
    #: The landmark at the destination ("" for a plain street spot), and its kind.
    landmark: str = ""
    kind: str = ""

    def view(self, origin: Point) -> dict[str, Any]:
        return {
            "landmark": self.landmark,
            "kind": self.kind,
            "near": self.near,
            "reward": self.reward,
            "distance_m": round(distance(origin, self.dest)),
        }


@dataclass
class Runner:
    """A Dodger in the round."""

    index: int
    name: str
    traveller: Traveller
    player: str | None = None
    bot: BotDodger | None = None
    caught: bool = False
    #: How they were caught: by whom (a human's id; ``None`` for a bot), on which route, or
    #: by the ticket inspectors.
    catcher: str | None = None
    route: str | None = None
    by_inspectors: bool = False
    package: Package | None = None
    deliveries: int = 0


@dataclass
class Human:
    """A human player in the round, on either side."""

    role: Role
    name: str
    traveller: Traveller
    keys: Keys = field(default_factory=Keys)
    events: list[Event] = field(default_factory=list)
    #: Their Dodger, when they run.
    runner: Runner | None = None


#: The human in a single-player round.
YOU = "you"


class LiveGame:
    """One round. ``runners`` and ``hunters`` are the two teams' seats (humans or bots); the
    Bounty Hunters also get ``cfg.hunter_extra`` more bots. Without seats it is a
    single-player round: you play ``role``, everyone else is a bot."""

    def __init__(
        self,
        world: World,
        cfg: LiveConfig,
        seed: int,
        role: Role = Role.DODGER,
        *,
        runners: list[Seat] | None = None,
        hunters: list[Seat] | None = None,
    ) -> None:
        self.world, self.cfg, self.seed = world, cfg, seed
        if runners is None or hunters is None:
            you, bot = Seat(YOU, "You"), Seat(None, "Bot")
            runners, hunters = ([you], [bot]) if role is Role.DODGER else ([bot], [you])
        self.t = float(cfg.start_seconds)
        self.status = Status.RUNNING
        self.pings: list[Ping] = []
        self.caught_by: str | None = None
        self.next_ping = cfg.start_seconds + cfg.ping_every_s
        rng = random.Random(seed)
        n_bot_hunters = sum(s.player is None for s in hunters) + cfg.hunter_extra
        human_hunters = [s for s in hunters if s.player is not None]
        runner_nodes, hunter_nodes = self._starts(
            rng, len(runners), len(human_hunters) + n_bot_hunters
        )
        xy, names = world.node_xy, world.tt.node_name
        # A separate stream, so the inspected trips do not change the start positions.
        inspected = random.Random(f"{seed}:inspected")
        self.inspected = frozenset(
            trip for trip in range(world.tt.n_trips) if inspected.random() < cfg.inspected_share
        )
        self.humans: dict[str, Human] = {}
        self.runners: list[Runner] = []
        self._parcels = random.Random(f"{seed}:packages")
        self.packages: list[Package] = []
        for i, (seat, node) in enumerate(zip(runners, runner_nodes, strict=True)):
            runner = Runner(
                i, seat.name, Traveller(xy[node], money=cfg.start_money_ft), seat.player
            )
            if seat.player is None:
                runner.bot = BotDodger(world, cfg, random.Random(f"{seed}:bot{i}"), i)
            else:
                self.humans[seat.player] = Human(
                    Role.DODGER, seat.name, runner.traveller, runner=runner
                )
            self.runners.append(runner)
        for seat, node in zip(human_hunters, hunter_nodes, strict=False):
            assert seat.player is not None
            self.humans[seat.player] = Human(
                Role.HUNTER, seat.name, Traveller(xy[node], pays_fares=False)
            )
        self.hunters = BountyHunters(world, cfg, hunter_nodes[len(human_hunters) :], rng)
        self.default_player = next(iter(self.humans), None)
        for _ in range(cfg.packages_waiting):
            self._add_package()
        for pid, human in self.humans.items():
            here = names[world.nearest_node(human.traveller.pos) or 0]
            if human.role is Role.DODGER:
                self._emit("start", f"You start at {here}. Stay hidden!", to=[pid])
            else:
                self._emit(
                    "start", f"You are a Bounty Hunter at {here}. Find the Dodgers!", to=[pid]
                )

    def _starts(
        self, rng: random.Random, n_runners: int, n_hunters: int
    ) -> tuple[list[int], list[int]]:
        """Random starts at stations with onward departures (not dead ends at the area edge):
        the Dodgers apart, every Bounty Hunter at least ``min_start_distance_min`` from every
        Dodger when possible."""
        world = self.world
        nodes = world.boarding_nodes or list(range(world.tt.n_nodes))
        runners = (
            rng.sample(nodes, n_runners)
            if len(nodes) >= n_runners
            else [rng.choice(nodes) for _ in range(n_runners)]
        )
        near = set(
            reachable(
                world.tt, runners, self.cfg.start_seconds, self.cfg.min_start_distance_min * 60
            ).reached()
        )
        far = [n for n in nodes if n not in near]
        if len(far) >= n_hunters:
            return runners, rng.sample(far, n_hunters)
        others = (
            sorted(
                (n for n in nodes if n not in runners),
                key=lambda n: (
                    -min(distance(world.node_xy[n], world.node_xy[r]) for r in runners),
                    n,
                ),
            )
            or runners
        )
        return runners, [others[i % len(others)] for i in range(n_hunters)]

    # --- the single-player shortcuts ------------------------------------------------------

    @property
    def dodger(self) -> Traveller:
        """The first Dodger (the only one in a single-player round)."""
        return self.runners[0].traveller

    @property
    def player(self) -> Traveller:
        """The default human's traveller."""
        return self.humans[self._pid(None)].traveller

    @property
    def role(self) -> Role:
        return self.humans[self._pid(None)].role

    @property
    def keys(self) -> Keys:
        return self.humans[self._pid(None)].keys

    @keys.setter
    def keys(self, keys: Keys) -> None:
        self.humans[self._pid(None)].keys = keys

    @property
    def dodger_bot(self) -> BotDodger | None:
        return self.runners[0].bot

    @dodger_bot.setter
    def dodger_bot(self, bot: BotDodger | None) -> None:
        self.runners[0].bot = bot

    @property
    def as_dodger(self) -> bool:
        return self.role is Role.DODGER

    def _pid(self, pid: str | None) -> str:
        found = pid if pid is not None else self.default_player
        if found is None or found not in self.humans:
            msg = f"no such player: {pid!r}"
            raise KeyError(msg)
        return found

    # --- messages -------------------------------------------------------------------------

    def _emit(self, kind: str, text: str, to: list[str] | None = None) -> None:
        """A message for some human players (everyone when ``to`` is ``None``)."""
        for pid in self.humans if to is None else to:
            self.humans[pid].events.append(Event(kind, text))

    def _announce(self, kind: str, to_runners: str, to_hunters: str) -> None:
        for human in self.humans.values():
            text = to_runners if human.role is Role.DODGER else to_hunters
            human.events.append(Event(kind, text))

    def _tell(self, who: Traveller, kind: str, text: str) -> None:
        """An event about ``who``, shown only to that human (bots are told nothing)."""
        for human in self.humans.values():
            if human.traveller is who:
                human.events.append(Event(kind, text))

    def drain_events(self, pid: str | None = None) -> list[Event]:
        human = self.humans[self._pid(pid)]
        events, human.events = human.events, []
        return events

    # --- packages (the Dodgers' side job) ---------------------------------------------------

    def _add_package(self) -> None:
        spot = self.world.random_spot(self._parcels)
        if spot is None:
            return
        dest = self._destination(spot)
        if dest is not None:
            self.packages.append(dest)

    def _destination(self, spot: Point) -> Package | None:
        """A package at ``spot`` with a destination: a landmark (a street spot in a district
        without any) ``package_min_m`` to ``package_max_m`` away, near a station that the
        timetable reaches from the package with one or two rides. Landmarks other packages
        already go to are skipped. Without such a place, the one nearest the distance range."""
        world, cfg, rng = self.world, self.cfg, self._parcels
        names, xy = world.tt.node_name, world.node_xy
        origin = world.nearest_node(spot)
        reached = (
            reachable(
                world.tt,
                [origin],
                int(self.t),
                PACKAGE_HORIZON_S,
                max_rounds=PACKAGE_MAX_RIDES,
            ).arrivals()
            if origin is not None
            else {}
        )
        taken = {p.landmark for p in self.packages} | {
            r.package.landmark for r in self.runners if r.package is not None
        }
        places = [(m.pos, m.name, m.kind) for m in world.landmarks if m.name not in taken]
        if not places:
            spots = (world.random_spot(rng) for _ in range(PACKAGE_STREET_SPOTS))
            places = [(p, "", "") for p in spots if p is not None]
        rng.shuffle(places)
        best: tuple[float, Point, str, str] | None = None
        for pos, name, kind in places:
            gap = distance(pos, spot)
            off = max(cfg.package_min_m - gap, gap - cfg.package_max_m, 0.0)
            node = world.nearest_node(pos)
            if node is None:
                continue
            if off == 0.0 and node in reached and distance(xy[node], pos) <= PACKAGE_STATION_M:
                best = (0.0, pos, name, kind)
                break
            if best is None or off < best[0]:
                best = (off, pos, name, kind)
        if best is None:
            return None
        _, pos, name, kind = best
        node = world.nearest_node(pos)
        near = names[node] if node is not None else "?"
        return Package(spot, cfg.package_reward_ft, pos, near, name, kind)

    def package_near(self, who: Traveller) -> Package | None:
        """A waiting package within reach of ``who`` (on foot), if any."""
        if who.ride is not None or who.held_until is not None:
            return None
        reach = self.cfg.package_radius_m
        return next((pkg for pkg in self.packages if distance(pkg.pos, who.pos) <= reach), None)

    def _accept_package(self, runner: Runner, pkg: Package) -> None:
        """``runner`` takes ``pkg``, and a new package appears elsewhere."""
        self.packages.remove(pkg)
        runner.package = pkg
        self._add_package()
        to = f"{pkg.landmark} (near {pkg.near})" if pkg.landmark else f"near {pkg.near}"
        self._tell(
            runner.traveller,
            "package",
            f"📦 Package accepted: take it to {to} for {pkg.reward} Ft.",
        )

    def _deliver(self) -> None:
        for runner in self.in_play():
            pkg, d = runner.package, runner.traveller
            if pkg is None or d.ride is not None:
                continue
            if distance(d.pos, pkg.dest) <= self.cfg.package_radius_m:
                d.money += pkg.reward
                runner.package = None
                runner.deliveries += 1
                self._tell(d, "package", f"📦 Delivered! +{pkg.reward} Ft.")

    # --- input (human players) -------------------------------------------------------------

    def set_keys(self, keys: Keys, pid: str | None = None) -> None:
        self.humans[self._pid(pid)].keys = keys

    def _active(self, pid: str | None) -> Human | None:
        """The human, if the round is on and they are still in it (not caught)."""
        human = self.humans[self._pid(pid)]
        if self.status is not Status.RUNNING:
            return None
        if human.runner is not None and human.runner.caught:
            return None
        return human

    def stop_of(self, who: Traveller) -> int | None:
        if who.ride is not None or who.held_until is not None or who.brewing_until is not None:
            return None
        return self.world.nearest_stop(who.pos, self.cfg.board_radius_m)

    def current_stop(self, pid: str | None = None) -> int | None:
        return self.stop_of(self.humans[self._pid(pid)].traveller)

    def departures(self, pid: str | None = None) -> list[Departure]:
        stop = self.current_stop(pid)
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

    def buy_coffee(self, pid: str | None = None) -> None:
        """Buy a coffee at the café: stand still while it is made, then run fast for a while."""
        human, cfg = self._active(pid), self.cfg
        if human is None or human.traveller.brewing_until is not None:
            return
        p, to = human.traveller, [self._pid(pid)]
        if not p.pays_fares:
            self._emit("info", "Bounty Hunters are on duty: no coffee breaks.", to)
            return
        cafe = self.cafe_at(p)
        if cafe is None:
            self._emit("info", "Find a café first (☕ on the map).", to)
        elif p.money < cfg.coffee_price_ft:
            self._emit("info", f"Not enough money: a coffee costs {cfg.coffee_price_ft} Ft.", to)
        else:
            p.money -= cfg.coffee_price_ft
            p.waiting_for = None
            p.brewing_until = self.t + cfg.coffee_brew_s
            self._emit(
                "coffee",
                f"One coffee at {self.world.cafes[cafe].name} ({cfg.coffee_price_ft} Ft), "
                "coming up…",
                to,
            )

    def buy_ticket(self, pid: str | None = None) -> None:
        """Buy a single ticket at the stop's machine."""
        human = self._active(pid)
        if human is None:
            return
        if not human.traveller.pays_fares:
            self._emit("info", "Bounty Hunters ride for free.", [self._pid(pid)])
            return
        self.buy_ticket_for(human.traveller)

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

    def use(self, pid: str | None = None) -> None:
        """The action key: get off (or stay on), stop waiting, or wait for the next one."""
        human = self._active(pid)
        if human is None:
            return
        p, to = human.traveller, [self._pid(pid)]
        if p.held_until is not None or p.brewing_until is not None:
            return
        runner = human.runner
        if runner is not None and runner.package is None and p.waiting_for is None:
            pkg = self.package_near(p)
            if pkg is not None:
                self._accept_package(runner, pkg)
                return
        if p.ride is not None:
            if p.ride.fined:
                self._emit("info", "The inspectors are taking you off at the next stop.", to)
                return
            if p.ride.alight_pos is not None:  # changed your mind: stay on
                p.ride.alight_pos = None
                self._emit("info", "You stay on.", to)
                return
            self._request_alight(p.ride, to)
        elif p.waiting_for is not None:
            p.waiting_for = None
            self._emit("info", "You stopped waiting.", to)
        else:
            self.wait_for(0, pid)

    def wait_for(self, index: int, pid: str | None = None) -> None:
        """Wait at the current stop for departure ``index`` of the listed departures."""
        human = self._active(pid)
        if human is None:
            return
        p, to = human.traveller, [self._pid(pid)]
        if p.ride is not None or p.held_until is not None:
            return
        if p.brewing_until is not None:
            self._emit("info", "Your coffee is nearly ready…", to)
            return
        departures = self.departures(pid)
        if self.current_stop(pid) is None:
            self._emit("info", "Walk to a stop first.", to)
        elif index >= len(departures):
            self._emit("info", "No such departure.", to)
        else:
            dep = departures[index]
            p.waiting_for = dep
            self._emit("info", f"Waiting for {dep.route} towards {dep.headsign}.", to)

    def _request_alight(self, ride: Ride, to: list[str]) -> None:
        state = self.world.trip_state(ride.trip, self.t)
        if state is None:
            return
        arr, dep = self.world.times(ride.trip)
        last = len(arr) - 1
        here = state.stop if self.t <= dep[state.stop] else state.stop + 1
        ride.alight_pos = min(max(here, ride.board_pos + 1), last)
        name = self.world.tt.node_name[self.world.stops(ride.trip)[ride.alight_pos]]
        self._emit("info", f"Getting off at {name}.", to)

    def make_bot(self, pid: str) -> None:
        """A human left: a bot plays their Dodger; a Bounty Hunter just stops."""
        human = self.humans.get(pid)
        if human is None:
            return
        human.keys = Keys()
        runner = human.runner
        if runner is not None and runner.bot is None:
            runner.player = None
            runner.bot = BotDodger(
                self.world, self.cfg, random.Random(f"{self.seed}:bot{runner.index}"), runner.index
            )

    # --- simulation ------------------------------------------------------------------------

    def step(self, dt: float) -> None:
        """Advance the game by ``dt`` timetable seconds."""
        while dt > 0 and self.status is Status.RUNNING:
            h = min(dt, MAX_SUBSTEP_S)
            self._advance(h)
            dt -= h

    def in_play(self) -> list[Runner]:
        return [r for r in self.runners if not r.caught]

    def _advance(self, h: float) -> None:
        self.t = min(self.t + h, self.cfg.end_seconds)
        for runner in self.in_play():
            if runner.bot is not None:
                target, direction = runner.bot.control(self)
                self._move(runner.traveller, h, direction, target)
            elif runner.player is not None:
                keys = self.humans[runner.player].keys
                self._move(runner.traveller, h, keys.direction(), None)
        for human in self.humans.values():
            if human.role is Role.HUNTER:
                self._move(human.traveller, h, human.keys.direction(), None)
        if self.status is not Status.RUNNING:
            return
        self.hunters.advance(
            self.t,
            h,
            [(r.traveller.pos, r.traveller.ride is None) for r in self.in_play()],
        )
        if self.t >= self.next_ping:
            self._ping()
        self._deliver()
        self._check_catch()
        if self.status is Status.RUNNING and self.t >= self.cfg.end_seconds:
            self.status = Status.ESCAPED
            for pid in self.humans:
                self._emit("end", self.summary(pid), [pid])
            self.caught_by = self.summary(self.default_player) if self.default_player else None

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
                who.follower.reset()
                who.rides += 1
                self._tell(who, "board", f"You boarded {dep.route} towards {dep.headsign}.")
                if who.pays_fares:
                    self._board(who, who.ride, dep)
                return
        boosted = who.boosted_until is not None
        step = (self.cfg.coffee_speed_mps if boosted else self.cfg.walk_speed_mps) * h
        if target is not None:  # bots: along the streets (straight without street data)
            old = who.pos
            if world.streets is not None:
                who.pos = world.walk_towards(who.follower, who.pos, target, step)
            else:
                who.pos = step_towards(who.pos, target, step)
            who.walked_m += distance(old, who.pos)
            return
        if direction is None or direction == (0.0, 0.0):
            return
        self._walk_free(who, step, direction)

    def _walk_free(self, who: Traveller, step: float, direction: Point) -> None:
        """Walk anywhere in the play area except into a building or the water (bridges are
        fine), sliding along walls.

        Starting inside a footprint (rare: a stop drawn inside a building) lets you walk out.
        """
        world, buildings, water = self.world, self.world.buildings, self.world.water
        inside = buildings.at(who.pos) if buildings is not None else None
        # In the water already (e.g. got off a ferry or a bridge edge): walk out freely.
        wet = water is not None and water.blocks(who.pos)

        def free(p: Point) -> bool:
            if not world.contains(p):
                return False
            if water is not None and not wet and water.blocks(p):
                return False
            if buildings is None:
                return True
            hit = buildings.at(p)
            return hit is None or hit == inside

        dx, dy = direction
        x, y = who.pos
        for angle in SLIDE_ANGLES:
            c, s = math.cos(angle), math.sin(angle)
            # Turned away from a wall, the move keeps only its share of the intended step.
            ux, uy = dx * c - dy * s, dx * s + dy * c
            candidate = (x + ux * step * c, y + uy * step * c)
            if free(candidate):
                who.walked_m += distance(who.pos, candidate)
                who.pos = candidate
                return

    def _runner_of(self, who: Traveller) -> Runner | None:
        return next((r for r in self.runners if r.traveller is who), None)

    def _board(self, d: Traveller, ride: Ride, dep: Departure) -> None:
        if d.tickets > 0:
            d.tickets -= 1
            ride.ticket = True
        if dep.trip not in self.inspected:
            return
        if ride.ticket:
            self._tell(d, "inspection", "Ticket inspection! Your ticket is valid. Phew.")
            return
        runner = self._runner_of(d)
        if d.money < self.cfg.fine_ft and runner is not None:
            self._catch(runner, inspectors=True)
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
                runner = self._runner_of(who)
                if self.cfg.fine_alerts_hunters and runner is not None:
                    self._report(runner, node, "The inspectors reported", "Inspectors fined")
            else:
                self._tell(who, "alight", f"You got off at {name}.")
            return
        state = world.trip_state(ride.trip, self.t)
        if state is not None:
            who.pos = state.pos

    def _called(self, runner: Runner) -> str:
        """How others refer to a Dodger."""
        return "the Dodger" if len(self.runners) == 1 else runner.name

    def _ping(self) -> None:
        self.next_ping += self.cfg.ping_every_s
        for runner in self.in_play():
            node = self.world.nearest_node(runner.traveller.pos)
            assert node is not None
            self._report(runner, node, "Spotted", "Spotted", tell_hunters=False)
        self.hunters.on_pings(self.latest_pings())
        names = ", ".join(self._called(r) for r in self.in_play())
        self._announce(
            "ping", "The Bounty Hunters got a report on your team.", f"New report: {names}."
        )

    def _report(
        self, runner: Runner, node: int, as_runner: str, as_hunter: str, tell_hunters: bool = True
    ) -> None:
        """Tell the Bounty Hunters where ``runner`` is."""
        self.pings.append(Ping(int(self.t), runner.traveller.pos, node, runner.index))
        place = self.world.tt.node_name[node]
        if runner.player is not None:
            self._emit("ping", f"{as_runner} you near {place}!", [runner.player])
        hunters = [pid for pid, h in self.humans.items() if h.role is Role.HUNTER]
        self._emit("ping", f"{as_hunter} {self._called(runner)} near {place}!", hunters)
        if tell_hunters:
            self.hunters.on_pings(self.latest_pings())

    def latest_pings(self) -> list[Ping]:
        """The latest report of each Dodger still in play."""
        latest: dict[int, Ping] = {}
        for ping in self.pings:
            latest[ping.runner] = ping
        return [latest[r.index] for r in self.in_play() if r.index in latest]

    def hunter_states(self) -> list[tuple[AgentState, str | None]]:
        """Every Bounty Hunter's state, and the human player's id (``None`` for bots)."""
        states: list[tuple[AgentState, str | None]] = [(s, None) for s in self.hunters.states()]
        for pid, human in self.humans.items():
            if human.role is Role.HUNTER:
                p = human.traveller
                trip = p.ride.trip if p.ride else None
                states.append((AgentState(p.pos, trip, None), pid))
        return states

    def visible(self, runner: Runner) -> bool:
        """The Bounty Hunters can see this Dodger (not riding, and one of them is close)."""
        d = runner.traveller
        if runner.caught or d.ride is not None:
            return False
        return any(
            distance(s.pos, d.pos) <= self.cfg.sight_radius_m for s, _ in self.hunter_states()
        )

    def dodger_visible(self) -> bool:
        return any(self.visible(r) for r in self.runners)

    def _check_catch(self) -> None:
        """A Bounty Hunter catches a Dodger on the same trip, or when both are on foot."""
        for runner in self.in_play():
            d = runner.traveller
            trip = d.ride.trip if d.ride else None
            for agent, pid in self.hunter_states():
                same_trip = trip is not None and agent.trip == trip
                on_foot_close = (
                    trip is None
                    and agent.trip is None
                    and distance(agent.pos, d.pos) <= self.cfg.catch_radius_m
                )
                if not (same_trip or on_foot_close):
                    continue
                route = self.world.route(trip) if same_trip and trip is not None else None
                self._catch(runner, catcher=pid, route=route)
                break

    def _catch(
        self,
        runner: Runner,
        *,
        catcher: str | None = None,
        route: str | None = None,
        inspectors: bool = False,
    ) -> None:
        """``runner`` is out: caught by ``catcher`` (a human; ``None`` for a bot), on
        ``route`` (same vehicle), or by the ticket inspectors. Their package is lost."""
        runner.caught, runner.catcher, runner.route = True, catcher, route
        runner.by_inspectors = inspectors
        runner.traveller.waiting_for = None
        runner.package = None
        for pid, human in self.humans.items():
            if pid == runner.player:
                self._emit("end", self._runner_line(runner), [pid])
            elif human.role is Role.DODGER:
                self._emit("ping", f"{runner.name} was caught!", [pid])
            else:
                self._emit("end", self._hunter_line(runner, pid), [pid])
        if not self.in_play():
            self.status = Status.CAUGHT
            self.caught_by = (
                self.summary(self.default_player)
                if self.default_player
                else "Every Dodger was caught."
            )

    def _runner_line(self, runner: Runner) -> str:
        """How a caught Dodger hears about it."""
        if runner.by_inspectors:
            return f"Ticket inspection! You can't pay the {self.cfg.fine_ft} Ft fine."
        if runner.route:
            return f"A Bounty Hunter was riding the {runner.route} with you."
        return "A Bounty Hunter caught you."

    def _hunter_line(self, runner: Runner, pid: str) -> str:
        """How a Bounty Hunter hears about a catch."""
        called = self._called(runner)
        if runner.by_inspectors:
            return f"Ticket inspectors caught {called}, who couldn't pay the fine."
        who = "You" if runner.catcher == pid else "Your team"
        where = f" on the {runner.route}" if runner.route else ""
        return f"{who} caught {called}{where}!"

    def summary(self, pid: str) -> str:
        """How the round went, told to one player (their own catches, their own escape)."""
        human = self.humans[pid]
        runner_side = human.role is Role.DODGER
        if len(self.runners) == 1:
            only = self.runners[0]
            if runner_side:
                return (
                    self._runner_line(only)
                    if only.caught
                    else "You made it! The Bounty Hunters gave up."
                )
            if only.caught:
                return self._hunter_line(only, pid)
            return f"Time's up: {self._called(only)} got away."
        lines = []
        for runner in self.runners:
            if runner is human.runner:
                lines.append(self._runner_line(runner) if runner.caught else "You got away!")
            elif runner_side:
                lines.append(f"{runner.name} {'was caught' if runner.caught else 'got away'}.")
            elif runner.caught:
                lines.append(self._hunter_line(runner, pid))
            else:
                lines.append(f"{runner.name} got away.")
        return " ".join(lines)

    def result(self) -> tuple[int, int]:
        """Points so far: (Dodgers still free, Dodgers caught). Final once the round is over."""
        caught = sum(r.caught for r in self.runners)
        return len(self.runners) - caught, caught

    def won(self, pid: str | None = None) -> bool | None:
        """Whether this human's side scored more this round (``None`` while running or tied)."""
        if self.status is Status.RUNNING:
            return None
        free, caught = self.result()
        if free == caught:
            return None
        return (free > caught) == (self.humans[self._pid(pid)].role is Role.DODGER)

    @property
    def player_won(self) -> bool | None:
        return self.won()

    # --- client view -----------------------------------------------------------------------

    def _latlon(self, p: Point) -> list[float]:
        lat, lon = self.world.proj.to_latlon(p)
        return [round(lat, 6), round(lon, 6)]

    def snapshot(self, pid: str | None = None) -> dict[str, Any]:
        """What one human player sees."""
        pid = self._pid(pid)
        human = self.humans[pid]
        world, p, t = self.world, human.traveller, self.t
        names = world.tt.node_name
        as_runner = human.role is Role.DODGER
        me = human.runner
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
        out = me is not None and me.caught
        stop = None if out else self.current_stop(pid)
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
                    for dep in self.departures(pid)
                ],
            }
        if out:
            state_name = "caught"
        elif p.ride:
            state_name = "ride"
        elif p.held_until is not None:
            state_name = "held"
        else:
            state_name = "wait" if waiting else "walk"
        over = self.status is not Status.RUNNING
        hunter_states = self.hunter_states()
        # Bounty Hunters: the bots, plus the human ones (as teammates or seen by the Dodgers).
        hunters_view = [
            {"pos": self._latlon(s.pos), "riding": s.trip is not None, "chasing": ag.chasing}
            for s, ag in zip(self.hunters.states(), self.hunters.agents, strict=True)
        ] + [
            {
                "pos": self._latlon(h.traveller.pos),
                "riding": h.traveller.ride is not None,
                "chasing": False,
                "name": h.name,
            }
            for other, h in self.humans.items()
            if h.role is Role.HUNTER and other != pid
        ]
        if as_runner:
            dodgers_view = [
                {
                    "pos": self._latlon(r.traveller.pos),
                    "held": r.traveller.held_until is not None,
                    "caught": r.caught,
                    "name": r.name,
                    "riding": r.traveller.ride is not None,
                }
                for r in self.runners
                if r is not me
            ]
        else:
            dodgers_view = [
                {
                    "pos": self._latlon(r.traveller.pos),
                    "held": r.traveller.held_until is not None,
                    "caught": r.caught,
                    "name": r.name,
                    "riding": r.traveller.ride is not None,
                }
                for r in self.runners
                if over or r.caught or self.visible(r)
            ]
        latest = self.latest_pings() if not as_runner else []
        last_ping = max(latest, key=lambda g: g.time) if latest else None
        d = me.traveller if me is not None else None
        return {
            "t": round(t, 1),
            "clock": format_hms(int(t)),
            "remaining_s": round(self.cfg.end_seconds - t),
            "next_ping_s": round(self.next_ping - t),
            "status": self.status.value,
            "role": human.role.value,
            "won": self.won(pid),
            "message": (
                self.summary(pid)
                if over
                else self._runner_line(me)
                if me is not None and me.caught
                else None
            ),
            "player": {"pos": self._latlon(p.pos), "state": state_name, "name": human.name},
            "wallet": {"money": p.money, "tickets": p.tickets} if p.pays_fares else None,
            "held_s": round(p.held_until - t) if p.held_until is not None else None,
            "coffee": {
                "cafe": (
                    self.world.cafes[cafe_here].name
                    if (cafe_here := self.cafe_at(p)) is not None and p.pays_fares and not out
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
            "hunters": hunters_view,
            "dodgers": dodgers_view,
            # The first one seen, for single-player views.
            "dodger": next((v for v in dodgers_view if not as_runner), None),
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
            "pings": [
                {
                    "pos": self._latlon(g.pos),
                    "age_s": round(t - g.time),
                    "name": self.runners[g.runner].name,
                }
                for g in latest
            ],
            "nearest_hunter_m": (
                round(min((distance(s.pos, d.pos) for s, _ in hunter_states), default=0.0))
                if d is not None
                else None
            ),
            "seen": self.visible(me) if me is not None else self.dodger_visible(),
            # Packages are the Dodgers' business: the Bounty Hunters never see them.
            "packages": [self._latlon(pkg.pos) for pkg in self.packages] if as_runner else [],
            # The package at your feet, and where it would go.
            "package_here": (
                here.view(p.pos)
                if as_runner
                and not out
                and me is not None
                and me.package is None
                and (here := self.package_near(p)) is not None
                else None
            ),
            "delivery": (
                {"pos": self._latlon(me.package.dest), **me.package.view(p.pos)}
                if me is not None and me.package is not None
                else None
            ),
            "runners": {
                "total": len(self.runners),
                "caught": sum(r.caught for r in self.runners),
            },
            "stats": {
                "walked_m": round(p.walked_m),
                "rides": p.rides,
                "tickets_bought": p.tickets_bought,
                "fines": p.fines,
                "money": p.money,
                "deliveries": me.deliveries if me is not None else 0,
            },
            "events": [{"kind": e.kind, "text": e.text} for e in self.drain_events(pid)],
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
        "package_reward_ft": cfg.package_reward_ft,
        "coffee_boost_s": cfg.coffee_boost_s,
    }

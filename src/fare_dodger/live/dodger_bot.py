"""The bot Dodger, for rounds where the human plays a Bounty Hunter.

The bot drives the Dodger's :class:`~fare_dodger.live.game.Traveller` through the same
mechanics as a human: it walks, waits at stops, boards, gets off, buys tickets or gambles,
and is fined like anyone else. Like a human Dodger it sees every Bounty Hunter, and it uses
that:

* It plans RAPTOR journeys to a station reachable within ``PLAN_REACH_S`` that is far from
  the Bounty Hunters and from its last reported position, skipping journeys that ride a trip a
  Bounty Hunter is on or stop near a Bounty Hunter on foot.
* At the destination it hides a little away from the station (the Bounty Hunters search
  stations) and dwells there before moving on.
* A Bounty Hunter on foot within ``FLEE_M`` makes it give up waiting and walk away (along the
  streets, steering like a human), unless its vehicle is about to leave; it replans once the
  threat is gone.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fare_dodger.config import LiveConfig
from fare_dodger.live.bots import leg_ends
from fare_dodger.live.world import Departure, Point, World, distance
from fare_dodger.routing.journey import Journey, RideLeg
from fare_dodger.routing.raptor import raptor, reachable

if TYPE_CHECKING:
    from fare_dodger.live.game import LiveGame

#: Candidate destinations: stations reachable within this long.
PLAN_REACH_S = 10 * 60
#: Journeys are searched this far ahead.
PLAN_HORIZON_S = 30 * 60
#: Best-scored destinations whose journeys are checked for Bounty Hunters on the way.
PLAN_CANDIDATES = 6
#: A journey stopping this close to a Bounty Hunter on foot is avoided (metres).
AVOID_M = 150.0
#: Weight of the distance from the last reported position, relative to the Bounty Hunters.
PING_WEIGHT = 0.5
#: Random jitter (metres) on destination scores, so the bot is not predictable.
JITTER_M = 150.0
#: Hiding spot distance from the destination station (metres).
HIDE_M = (90.0, 140.0)
#: Time spent hiding before moving on (timetable seconds).
DWELL_S = (20.0, 90.0)
#: A Bounty Hunter on foot this close makes the bot flee (metres).
FLEE_M = 220.0
#: Keep waiting despite a threat if the vehicle leaves this soon (s) and the threat is farther
#: than this (m).
STAY_IF_LEAVING_S = 20.0
STAY_IF_FARTHER_M = 60.0
#: Replans happen at most this often (timetable seconds).
REPLAN_GAP_S = 5.0
#: Chance of buying a ticket before a ride (if it has none and can afford one).
TICKET_PROB = 0.5


@dataclass(frozen=True, slots=True)
class Step:
    """Walk to ``point``, or ride ``leg`` (``leg`` set, ``point`` is where it ends)."""

    point: Point
    leg: RideLeg | None = None


class BotDodger:
    def __init__(self, world: World, cfg: LiveConfig, rng: random.Random, runner: int = 0) -> None:
        #: Which of the round's Dodgers this bot plays.
        self.runner = runner
        self.world = world
        self.cfg = cfg
        self.rng = rng
        self.steps: list[Step] = []
        self.next_plan = 0.0
        self.last_plan = -1e9
        #: Seen aboard since the current ride step began.
        self.riding = False

    def control(self, game: LiveGame) -> tuple[Point | None, Point | None]:
        """Act for this substep; returns the point to walk towards or the direction to walk
        in (unit vector), if any."""
        self.flee_dir: Point | None = None
        target = self._target(game)
        return target, self.flee_dir

    def _target(self, game: LiveGame) -> Point | None:
        d, t = game.runners[self.runner].traveller, game.t
        if d.held_until is not None:
            self.steps, self.riding = [], False
            self.next_plan = d.held_until
            return None
        if d.ride is not None:
            self.riding = True
            self._set_alight(game)
            return None
        if self.riding:  # just got off
            self.riding = False
            step = self.steps.pop(0) if self.steps else None
            if step is None or distance(d.pos, step.point) > 0.5:
                self.steps = []
            elif not self.steps:
                self.next_plan = t + self.rng.uniform(*DWELL_S)

        self.flee_dir = self._flee(game)
        if self.flee_dir is not None:
            return None
        if not self.steps and t >= self.next_plan:
            self._plan(game)
        if not self.steps:
            return None
        step = self.steps[0]
        if step.leg is None:
            if distance(d.pos, step.point) < 0.5:
                self.steps.pop(0)
                if not self.steps:
                    self.next_plan = t + self.rng.uniform(*DWELL_S)
                return None
            return step.point
        return self._board(game, step.leg)

    def _set_alight(self, game: LiveGame) -> None:
        ride = game.runners[self.runner].traveller.ride
        leg = self.steps[0].leg if self.steps else None
        if (
            ride is not None
            and leg is not None
            and not ride.fined
            and ride.trip == leg.trip
            and ride.alight_pos is None
        ):
            ride.alight_pos = leg.alight_pos

    def _board(self, game: LiveGame, leg: RideLeg) -> Point | None:
        d, t, world = game.runners[self.runner].traveller, game.t, self.world
        if t > leg.dep or any(s.trip == leg.trip for s, _ in game.hunter_states()):
            self.steps = []  # missed it, or a Bounty Hunter is aboard
            self.next_plan = t
            d.waiting_for = None
            return None
        stop = world.stop_point(leg.pattern, leg.board_pos)
        if distance(d.pos, stop) > 0.5:
            return stop
        if d.waiting_for is None:
            if d.tickets == 0 and self.rng.random() < TICKET_PROB:
                game.buy_ticket_for(d)
            d.waiting_for = Departure(
                leg.trip,
                leg.pattern,
                leg.board_pos,
                leg.from_node,
                leg.dep,
                leg.route_name,
                leg.mode,
                world.headsign(leg.pattern),
            )
        return None

    def _flee(self, game: LiveGame) -> Point | None:
        """The direction away from the closest Bounty Hunter on foot, if one is too close."""
        d, t = game.runners[self.runner].traveller, game.t
        threats = [s.pos for s, _ in game.hunter_states() if s.trip is None]
        if not threats:
            return None
        near = min(threats, key=lambda p: distance(p, d.pos))
        gap = distance(near, d.pos)
        if gap > FLEE_M:
            return None
        dep = d.waiting_for
        if dep is not None and dep.time - t <= STAY_IF_LEAVING_S and gap > STAY_IF_FARTHER_M:
            return None
        d.waiting_for = None
        self.steps, self.next_plan = [], t
        if gap < 1e-6:
            angle = self.rng.uniform(0, 2 * math.pi)
            away = (math.cos(angle), math.sin(angle))
        else:
            away = ((d.pos[0] - near[0]) / gap, (d.pos[1] - near[1]) / gap)
        return away

    def _plan(self, game: LiveGame) -> None:
        t = game.t
        if t - self.last_plan < REPLAN_GAP_S:
            return
        self.last_plan = t
        self.steps, self.riding = [], False
        world, d = self.world, game.runners[self.runner].traveller
        tt, xy = world.tt, world.node_xy
        origin = world.nearest_node(d.pos)
        if origin is None:
            return
        start = int(t)
        reached = set(reachable(tt, [origin], start, PLAN_REACH_S).reached())
        candidates = [n for n in world.boarding_nodes if n in reached and n != origin]
        if not candidates:
            return
        hunters = game.hunter_states()
        on_foot = [s.pos for s, _ in hunters if s.trip is None]
        hunter_trips = {s.trip for s, _ in hunters if s.trip is not None}
        own = [p for p in game.pings if p.runner == self.runner]
        ping = own[-1].pos if own else None

        def score(n: int) -> float:
            near = min((distance(xy[n], s.pos) for s, _ in hunters), default=0.0)
            away = distance(xy[n], ping) if ping is not None else 0.0
            return near + PING_WEIGHT * away + self.rng.uniform(0, JITTER_M)

        def safe(journey: Journey) -> bool:
            for leg in journey.rides:
                if leg.trip in hunter_trips:
                    return False
                if any(
                    distance(xy[node], h) < AVOID_M
                    for node in (leg.from_node, leg.to_node)
                    for h in on_foot
                ):
                    return False
            return True

        result = raptor(tt, [origin], start, start + PLAN_HORIZON_S)
        best: Journey | None = None
        for target in sorted(candidates, key=score, reverse=True)[:PLAN_CANDIDATES]:
            if result.arrival(target) is None:
                continue
            journey = result.journey(target)
            best = best or journey
            if safe(journey):
                best = journey
                break
        if best is None:
            return
        self.steps = [
            Step(end, leg) if isinstance(leg, RideLeg) else Step(end)
            for leg, (_, end) in zip(best.legs, leg_ends(world, best), strict=True)
        ]
        self.steps.append(Step(self._hiding_spot(xy[best.destination])))

    def _hiding_spot(self, station: Point) -> Point:
        """A point a little away from ``station`` inside the play area."""
        for _ in range(8):
            angle = self.rng.uniform(0, 2 * math.pi)
            r = self.rng.uniform(*HIDE_M)
            spot = (station[0] + r * math.cos(angle), station[1] + r * math.sin(angle))
            if self.world.contains(spot):
                streets = self.world.streets
                return spot if streets is None else streets.point(streets.snap(spot))
        return station

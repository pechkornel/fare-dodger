"""Bot Bounty Hunters for the live game.

Each Bounty Hunter follows a RAPTOR journey (walk and ride legs on real trips) to a target
station. Targets come from what the team knows: before the first ping they spread over the
district. After a ping they cover the stations the Dodger could have reached since then
(farthest-point spread, starting at the pinged station). A Bounty Hunter on foot who sees the
Dodger on foot gives chase directly, and walks back to the network when it loses sight.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from fare_dodger.config import LiveConfig
from fare_dodger.live.streets import RouteFollower
from fare_dodger.live.world import Point, World, distance
from fare_dodger.routing.journey import Journey, RideLeg
from fare_dodger.routing.raptor import raptor, reachable

#: Stations the Dodger could reach this long after a ping are covered by the first targets.
PING_SPREAD_S = 300
#: Extra look-ahead when idle Bounty Hunters pick a new station to search.
SEARCH_LOOKAHEAD_S = 120
#: Journeys longer than this are not planned.
PLAN_HORIZON_S = 3600
#: A Bounty Hunter with nowhere to go waits this long before looking again.
IDLE_WAIT_S = 20


@dataclass(frozen=True, slots=True)
class Ping:
    time: int
    pos: Point
    node: int


@dataclass(frozen=True, slots=True)
class AgentState:
    pos: Point
    #: Trip the agent is riding, if any.
    trip: int | None
    #: Station the agent is standing at (waiting or idle), if any.
    node: int | None


def leg_ends(world: World, journey: Journey) -> list[tuple[Point, Point]]:
    """Where each leg of ``journey`` starts and ends: rides at their vehicles' stop points,
    walks between the neighbouring rides (or station centres)."""
    legs = journey.legs
    rides: list[tuple[Point, Point] | None] = [
        (
            world.stop_point(leg.pattern, leg.board_pos),
            world.stop_point(leg.pattern, leg.alight_pos),
        )
        if isinstance(leg, RideLeg)
        else None
        for leg in legs
    ]
    ends: list[tuple[Point, Point]] = []
    for k, leg in enumerate(legs):
        ride = rides[k]
        if ride is not None:
            ends.append(ride)
            continue
        start = ends[-1][1] if ends else world.node_xy[leg.from_node]
        after = rides[k + 1] if k + 1 < len(legs) else None
        ends.append((start, after[0] if after is not None else world.node_xy[leg.to_node]))
    return ends


def journey_state(world: World, journey: Journey, t: float) -> AgentState:
    """Where someone following ``journey`` is at time ``t``."""
    ends = leg_ends(world, journey)
    for leg, (start, end) in zip(journey.legs, ends, strict=True):
        if t < leg.dep:
            return AgentState(start, None, leg.from_node)
        if t <= leg.arr:
            if isinstance(leg, RideLeg):
                state = world.trip_state(leg.trip, t)
                return AgentState(state.pos if state else end, leg.trip, None)
            f = (t - leg.dep) / (leg.arr - leg.dep) if leg.arr > leg.dep else 1.0
            return AgentState(world.walk_point(start, end, f), None, None)
    dest = journey.destination
    return AgentState(ends[-1][1] if ends else world.node_xy[dest], None, dest)


@dataclass
class Agent:
    node: int
    pos: Point
    target: int
    plan: Journey | None = None
    needs_replan: bool = True
    chasing: bool = False
    #: Walking back to this station after a chase.
    returning_to: int | None = None
    trip: int | None = field(default=None)
    #: Idle until this time (no journey found).
    idle_until: float = 0.0
    #: Street route while chasing or walking back.
    follower: RouteFollower = field(default_factory=RouteFollower)


class BountyHunters:
    def __init__(
        self, world: World, cfg: LiveConfig, starts: list[int], rng: random.Random
    ) -> None:
        self.world = world
        self.cfg = cfg
        self.rng = rng
        self.last_ping: Ping | None = None
        targets = self._spread(list(range(world.tt.n_nodes)), len(starts), seed_node=None)
        self.agents = [
            Agent(node, world.node_xy[node], target)
            for node, target in zip(starts, self._assign(starts, targets), strict=True)
        ]

    # --- knowledge -------------------------------------------------------------------------

    def on_ping(self, ping: Ping) -> None:
        self.last_ping = ping
        spread = reachable(self.world.tt, [ping.node], ping.time, PING_SPREAD_S).reached()
        targets = self._spread(spread or [ping.node], len(self.agents), seed_node=ping.node)
        origins = [self._planning_node(a) for a in self.agents]
        for agent, target in zip(self.agents, self._assign(origins, targets), strict=True):
            agent.target = target
            agent.needs_replan = True

    def _spread(self, nodes: list[int], k: int, seed_node: int | None) -> list[int]:
        """``k`` stations spread over ``nodes`` (farthest-point sampling)."""
        xy = self.world.node_xy
        if k == 0:
            return []
        if seed_node is None:
            cx = sum(xy[n][0] for n in nodes) / len(nodes)
            cy = sum(xy[n][1] for n in nodes) / len(nodes)
            seed_node = min(nodes, key=lambda n: (distance(xy[n], (cx, cy)), n))
        chosen = [seed_node]
        while len(chosen) < k:
            candidates = [n for n in nodes if n not in chosen]
            if not candidates:
                chosen.append(chosen[len(chosen) % len(chosen)])
                continue
            chosen.append(
                max(candidates, key=lambda n: (min(distance(xy[n], xy[c]) for c in chosen), -n))
            )
        return chosen

    def _assign(self, origins: list[int], targets: list[int]) -> list[int]:
        """Greedy nearest assignment: each target to the closest unassigned Bounty Hunter."""
        xy = self.world.node_xy
        result = [-1] * len(origins)
        free = set(range(len(origins)))
        for target in targets:
            best = min(free, key=lambda i: (distance(xy[origins[i]], xy[target]), i))
            result[best] = target
            free.discard(best)
        return result

    def _planning_node(self, agent: Agent) -> int:
        if agent.plan is not None:
            return agent.plan.destination
        return agent.returning_to if agent.returning_to is not None else agent.node

    # --- movement --------------------------------------------------------------------------

    def advance(self, t: float, dt: float, dodger: Point, dodger_on_foot: bool) -> None:
        step = self.cfg.walk_speed_mps * dt
        for agent in self.agents:
            on_foot = agent.trip is None
            if (
                dodger_on_foot
                and on_foot
                and distance(agent.pos, dodger) <= self.cfg.sight_radius_m
            ):
                if not agent.chasing:
                    agent.follower.reset()
                agent.chasing, agent.plan, agent.returning_to = True, None, None
                agent.pos = self.world.walk_towards(agent.follower, agent.pos, dodger, step)
                continue
            if agent.chasing:
                agent.chasing = False
                nearest = self.world.nearest_node(agent.pos)
                agent.returning_to = nearest if nearest is not None else agent.node
                agent.follower.reset()
            if agent.returning_to is not None:
                home = self.world.node_xy[agent.returning_to]
                agent.pos = self.world.walk_towards(agent.follower, agent.pos, home, step)
                if agent.pos == home:
                    agent.node, agent.returning_to = agent.returning_to, None
                    agent.needs_replan = True
                continue
            self._follow(agent, t)

    def _follow(self, agent: Agent, t: float) -> None:
        if agent.plan is not None:
            state = journey_state(self.world, agent.plan, t)
            agent.pos, agent.trip = state.pos, state.trip
            if state.node is not None:
                agent.node = state.node
            if t >= agent.plan.arrival:
                agent.plan = None
            elif not (agent.needs_replan and state.node is not None):
                return
        if agent.plan is None and not agent.needs_replan:
            if t < agent.idle_until:
                return
            agent.target = self._search_target(t)
        agent.plan = self._plan(agent.node, agent.target, t)
        agent.needs_replan = False
        if agent.plan is None:
            agent.pos, agent.trip = self.world.node_xy[agent.node], None
            agent.idle_until = t + IDLE_WAIT_S

    def _search_target(self, t: float) -> int:
        """A station the Dodger could be at now, given the last ping (any before one)."""
        tt = self.world.tt
        if self.last_ping is None:
            return self.rng.randrange(tt.n_nodes)
        horizon = int(t) - self.last_ping.time + SEARCH_LOOKAHEAD_S
        nodes = reachable(tt, [self.last_ping.node], self.last_ping.time, horizon).reached()
        return self.rng.choice(nodes) if nodes else self.last_ping.node

    def _plan(self, origin: int, target: int, t: float) -> Journey | None:
        if origin == target:
            return None
        start = int(t)
        result = raptor(self.world.tt, [origin], start, start + PLAN_HORIZON_S)
        if result.arrival(target) is None:
            return None
        return result.journey(target)

    def states(self) -> list[AgentState]:
        return [
            AgentState(a.pos, a.trip, a.node if a.trip is None and a.plan is None else None)
            for a in self.agents
        ]

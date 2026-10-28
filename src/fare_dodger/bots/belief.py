"""Inspectors that track where the Dodger could be and spread out to cover it.

The belief is the set of stations the Dodger could occupy at the start of the current round.
It is a *sound* over-approximation: it never excludes the Dodger's true position.

* Start: every station that no Inspector can reach within the minimum start distance
  (the rules guarantee the Dodger starts there).
* After each round: expand by everything reachable within one round (a single multi-source
  RAPTOR query). If modes were revealed, only those vehicle modes may be used, and at
  most as many rides as were revealed. Then remove the stations where Inspectors ended the
  round without a catch.
* After a reveal: collapse to the revealed station.

To move, the team predicts the Dodger's possible positions at the end of this round, splits
them into one geographic cluster per Inspector (seeded k-means), assigns Inspectors to
clusters by travel time, and sends each Inspector to the reachable station closest to its
cluster's centre, preferring stations inside the cluster where a catch is possible.
"""

from __future__ import annotations

import itertools
import math

from fare_dodger.bots.base import bot_rng, round_reach
from fare_dodger.game.engine import InspectorView
from fare_dodger.geo import metres_per_degree
from fare_dodger.modes import ALL_VEHICLES_MASK, Mode, mask_of
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey
from fare_dodger.routing.raptor import reachable

Point = tuple[float, float]


class BeliefState:
    """Possible Dodger stations, updated from the public information in each view."""

    def __init__(self, tt: Timetable) -> None:
        self.tt = tt
        self.nodes: frozenset[int] = frozenset()
        self.rounds_seen = 0
        self.history: list[int] = []

    def initialise(self, view: InspectorView) -> None:
        everything = frozenset(range(self.tt.n_nodes))
        horizon = view.rules.min_start_distance_min * 60
        if horizon:
            near = reachable(self.tt, view.positions, view.window[0], horizon).arrivals()
            candidates = everything - near.keys()
            self.nodes = candidates or everything
        else:
            self.nodes = everything - set(view.positions) or everything
        self.history.append(len(self.nodes))

    def update(self, view: InspectorView) -> None:
        """Advance the belief to the start of ``view.round_number``."""
        if self.rounds_seen == 0 and view.round_number == 1:
            self.initialise(view)
        while self.rounds_seen < view.round_number - 1:
            done = self.rounds_seen  # 0-based index of the completed round to apply
            window = view.rules.round_window(done)
            self.nodes = self._expand(self.nodes, window, view.mode_log[done])
            self.nodes -= set(view.position_history[done])
            if view.last_reveal is not None and view.last_reveal[0] == done + 1:
                self.nodes = frozenset({view.last_reveal[1]})
            if not self.nodes:  # cannot happen with sound updates; recover anyway
                self.nodes = frozenset(range(self.tt.n_nodes))
            self.rounds_seen += 1
            self.history.append(len(self.nodes))

    def _expand(
        self, nodes: frozenset[int], window: tuple[int, int], modes: tuple[Mode, ...] | None
    ) -> frozenset[int]:
        if modes is not None and not modes:
            return nodes  # the Dodger stayed put
        mask, rides = ALL_VEHICLES_MASK, 8
        if modes is not None:
            mask = mask_of(set(modes))
            rides = sum(1 for m in modes if m is not Mode.WALK)
        result = reachable(
            self.tt, nodes, window[0], window[1] - window[0], mode_mask=mask, max_rounds=rides
        )
        return frozenset(result.arrivals())

    def predict(self, window: tuple[int, int]) -> frozenset[int]:
        """Where the Dodger could be at the end of the round starting now."""
        return self._expand(self.nodes, window, None)


class BeliefInspectors:
    name = "belief"

    def __init__(self, tt: Timetable, seed: int, kmeans_iterations: int = 12) -> None:
        self.tt = tt
        self.rng = bot_rng(seed, "inspectors")
        self.belief = BeliefState(tt)
        self.iterations = kmeans_iterations
        m_lat, m_lon = metres_per_degree(sum(tt.node_lat) / max(1, tt.n_nodes))
        self.xy: list[Point] = [
            (lat * m_lat, lon * m_lon) for lat, lon in zip(tt.node_lat, tt.node_lon, strict=True)
        ]

    def move(self, view: InspectorView) -> tuple[Journey, ...]:
        self.belief.update(view)
        target_area = sorted(self.belief.predict(view.window))
        reaches = [round_reach(self.tt, p, view.window) for p in view.positions]
        k = len(view.positions)
        centres, clusters = self._kmeans(target_area, k)

        # Assign Inspectors to clusters minimising total travel time to the cluster centres.
        far = [reachable(self.tt, [p], view.window[0], 3600).arrivals() for p in view.positions]
        anchors = [self._nearest(centre, target_area) for centre in centres]

        def cost(i: int, c: int) -> int:
            return far[i].get(anchors[c], view.window[0] + 7200)

        assignment = min(
            itertools.permutations(range(len(centres)), min(k, len(centres))),
            key=lambda perm: (sum(cost(i, c) for i, c in enumerate(perm)), perm),
        )

        taken: set[int] = set()
        journeys: list[Journey] = []
        for i, result in enumerate(reaches):
            options = [n for n in sorted(result.reached()) if n not in taken]
            if i < len(assignment):
                c = assignment[i]
                inside = [n for n in options if n in clusters[c]]
                pool = inside or options
                target = min(pool, key=lambda n: (self._dist(self.xy[n], centres[c]), n))
            else:
                target = self.rng.choice(options)
            taken.add(target)
            journeys.append(result.journey(target))
        return tuple(journeys)

    # --- geometry helpers -------------------------------------------------------------
    @staticmethod
    def _dist(a: Point, b: Point) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    def _nearest(self, centre: Point, nodes: list[int]) -> int:
        return min(nodes, key=lambda n: (self._dist(self.xy[n], centre), n))

    def _kmeans(self, nodes: list[int], k: int) -> tuple[list[Point], list[set[int]]]:
        """Seeded Lloyd's k-means on station coordinates (deterministic for a seed)."""
        k = max(1, min(k, len(nodes)))
        centres = [self.xy[n] for n in self.rng.sample(nodes, k)]
        groups: list[set[int]] = [set() for _ in range(k)]
        for _ in range(self.iterations):
            groups = [set() for _ in range(k)]
            for n in nodes:
                best = min(range(k), key=lambda c: (self._dist(self.xy[n], centres[c]), c))
                groups[best].add(n)
            new_centres = []
            for c in range(k):
                if groups[c]:
                    xs = [self.xy[n][0] for n in groups[c]]
                    ys = [self.xy[n][1] for n in groups[c]]
                    new_centres.append((sum(xs) / len(xs), sum(ys) / len(ys)))
                else:
                    new_centres.append(centres[c])
            if new_centres == centres:
                break
            centres = new_centres
        return centres, groups

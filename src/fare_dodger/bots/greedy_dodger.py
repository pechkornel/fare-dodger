"""A Dodger that runs as far (in travel time) from the Inspectors as it can each round."""

from __future__ import annotations

from fare_dodger.bots.base import bot_rng, round_reach
from fare_dodger.game.engine import DodgerView
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey
from fare_dodger.routing.raptor import reachable


class GreedyDodger:
    """Maximise the minimum travel time from the Inspectors' last known positions.

    Candidates are all stations reachable this round. Each is scored by the earliest time any
    Inspector could get there leaving at the round start (one RAPTOR query per Inspector with
    a ``horizon_min`` budget; unreachable counts as ``horizon + 1``). Ties are broken by the
    sum over Inspectors, then at random.
    """

    name = "greedy"

    def __init__(self, tt: Timetable, seed: int, horizon_min: int = 45) -> None:
        self.tt = tt
        self.rng = bot_rng(seed, "dodger")
        self.horizon = horizon_min * 60
        self.last_known: tuple[int, ...] | None = None

    def move(self, view: DodgerView) -> Journey:
        own = round_reach(self.tt, view.position, view.window)
        candidates = sorted(own.reached())
        if view.inspector_positions is not None:
            self.last_known = view.inspector_positions
        if self.last_known is None:
            return own.journey(self.rng.choice(candidates))

        unreached = self.horizon + 1
        chasers = [
            reachable(self.tt, [p], view.window[0], self.horizon).arrivals()
            for p in self.last_known
        ]
        scored: dict[tuple[int, int], list[int]] = {}
        for node in candidates:
            times = [c[node] - view.window[0] if node in c else unreached for c in chasers]
            scored.setdefault((min(times), sum(times)), []).append(node)
        best = max(scored)
        return own.journey(self.rng.choice(scored[best]))

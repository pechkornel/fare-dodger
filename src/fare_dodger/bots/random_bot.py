"""Baseline bots that pick a uniformly random reachable station."""

from __future__ import annotations

from fare_dodger.bots.base import bot_rng, round_reach
from fare_dodger.game.engine import DodgerView, InspectorView
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey


class RandomDodger:
    name = "random"

    def __init__(self, tt: Timetable, seed: int) -> None:
        self.tt = tt
        self.rng = bot_rng(seed, "dodger")

    def move(self, view: DodgerView) -> Journey:
        result = round_reach(self.tt, view.position, view.window)
        return result.journey(self.rng.choice(sorted(result.reached())))


class RandomInspectors:
    name = "random"

    def __init__(self, tt: Timetable, seed: int) -> None:
        self.tt = tt
        self.rng = bot_rng(seed, "inspectors")

    def move(self, view: InspectorView) -> tuple[Journey, ...]:
        journeys = []
        for position in view.positions:
            result = round_reach(self.tt, position, view.window)
            journeys.append(result.journey(self.rng.choice(sorted(result.reached()))))
        return tuple(journeys)

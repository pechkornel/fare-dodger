"""Bots for both roles, addressable by name."""

from __future__ import annotations

from collections.abc import Callable

from fare_dodger.bots.base import DodgerBot, InspectorBot
from fare_dodger.bots.belief import BeliefInspectors, KMeansBeliefInspectors
from fare_dodger.bots.greedy_dodger import GreedyDodger
from fare_dodger.bots.random_bot import RandomDodger, RandomInspectors
from fare_dodger.network.timetable import Timetable

DODGER_BOTS: dict[str, Callable[[Timetable, int], DodgerBot]] = {
    "random": RandomDodger,
    "greedy": GreedyDodger,
}
INSPECTOR_BOTS: dict[str, Callable[[Timetable, int], InspectorBot]] = {
    "random": RandomInspectors,
    "belief": BeliefInspectors,
    "belief-kmeans": KMeansBeliefInspectors,
}

__all__ = [
    "DODGER_BOTS",
    "INSPECTOR_BOTS",
    "BeliefInspectors",
    "DodgerBot",
    "GreedyDodger",
    "InspectorBot",
    "KMeansBeliefInspectors",
    "RandomDodger",
    "RandomInspectors",
]

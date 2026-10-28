"""Bot interfaces.

A bot is created per game with the timetable and a seed. It only ever sees the view object of
its role, so it cannot cheat. All randomness comes from its own seeded ``random.Random``.
"""

from __future__ import annotations

import random
from typing import Protocol

from fare_dodger.game.engine import DodgerView, InspectorView
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey
from fare_dodger.routing.raptor import RaptorResult, reachable


class DodgerBot(Protocol):
    name: str

    def move(self, view: DodgerView) -> Journey: ...


class InspectorBot(Protocol):
    """Controls the whole Inspector team (so the Inspectors can coordinate)."""

    name: str

    def move(self, view: InspectorView) -> tuple[Journey, ...]: ...


def bot_rng(seed: int, role: str) -> random.Random:
    """Independent, reproducible stream per (game seed, role)."""
    return random.Random(f"{seed}:{role}")


def round_reach(tt: Timetable, node: int, window: tuple[int, int]) -> RaptorResult:
    """Everything reachable from ``node`` within the round."""
    return reachable(tt, [node], window[0], window[1] - window[0])

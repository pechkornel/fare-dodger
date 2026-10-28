"""The game loop: simultaneous rounds, catches, reveals and player views.

Each round every player submits a journey privately; then all journeys are validated and
resolved at once (see ADR 0003). Information hiding is enforced through the view objects:
bots only ever receive a :class:`DodgerView` or an :class:`InspectorView`, never the game.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from fare_dodger.config import RulesConfig
from fare_dodger.game.validate import validate_journey
from fare_dodger.modes import Mode
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, RideLeg
from fare_dodger.routing.raptor import reachable


class GameStatus(StrEnum):
    RUNNING = "running"
    DODGER_WON = "dodger_won"
    INSPECTORS_WON = "inspectors_won"


@dataclass(frozen=True, slots=True)
class Catch:
    round_number: int
    inspector: int
    kind: Literal["stop", "vehicle"]
    node: int
    time: int
    trip: int | None = None


@dataclass(frozen=True, slots=True)
class RoundRecord:
    round_number: int
    window: tuple[int, int]
    dodger_journey: Journey
    inspector_journeys: tuple[Journey, ...]
    catches: tuple[Catch, ...]
    #: Dodger position announced to the Inspectors after this round (if a reveal round).
    revealed_node: int | None
    #: Dodger's modes announced to the Inspectors (if mode reveals are on).
    revealed_modes: tuple[Mode, ...] | None


@dataclass(frozen=True, slots=True)
class DodgerView:
    rules: RulesConfig
    round_number: int
    window: tuple[int, int]
    position: int
    #: Current Inspector positions, ``None`` when Inspectors are hidden.
    inspector_positions: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class InspectorView:
    rules: RulesConfig
    round_number: int
    window: tuple[int, int]
    positions: tuple[int, ...]
    #: ``(round_number, node)`` of the most recent reveal (0 = start, if ever revealed).
    last_reveal: tuple[int, int] | None
    #: Per completed round: the Dodger's revealed modes, or ``None`` when not revealed.
    mode_log: tuple[tuple[Mode, ...] | None, ...]
    #: Inspector end positions per completed round (where the Dodger was *not*).
    position_history: tuple[tuple[int, ...], ...]


def vehicle_overlap(a: RideLeg, b: RideLeg) -> tuple[int, int] | None:
    """Shared hop range ``[lo, hi)`` of two rides on the same trip, if they overlap."""
    if a.trip != b.trip:
        return None
    lo, hi = max(a.board_pos, b.board_pos), min(a.alight_pos, b.alight_pos)
    return (lo, hi) if lo < hi else None


def find_catches(
    tt: Timetable,
    rules: RulesConfig,
    round_number: int,
    dodger: Journey,
    inspectors: tuple[Journey, ...],
    round_end: int,
) -> tuple[Catch, ...]:
    """All catches of a resolved round, ordered by time then Inspector."""
    catches: list[Catch] = []
    for idx, journey in enumerate(inspectors):
        if rules.catch_on_same_vehicle:
            for d_ride in dodger.rides:
                for i_ride in journey.rides:
                    overlap = vehicle_overlap(d_ride, i_ride)
                    if overlap is None:
                        continue
                    pos = overlap[0]
                    off = tt.trip_time_ptr[d_ride.trip]
                    catches.append(
                        Catch(
                            round_number=round_number,
                            inspector=idx,
                            kind="vehicle",
                            node=tt.pattern_stops(d_ride.pattern)[pos],
                            time=tt.trip_dep[off + pos],
                            trip=d_ride.trip,
                        )
                    )
        if rules.catch_on_same_stop and journey.destination == dodger.destination:
            catches.append(Catch(round_number, idx, "stop", dodger.destination, round_end))
    catches.sort(key=lambda c: (c.time, c.inspector, c.kind))
    return tuple(catches)


def random_start(
    tt: Timetable, rules: RulesConfig, rng: random.Random, max_attempts: int = 2000
) -> tuple[int, tuple[int, ...]]:
    """Random Dodger start and Inspector starts at least ``min_start_distance_min`` away.

    The distance is the travel time *from* each Inspector *to* the Dodger at the start time.
    """
    start = rules.start_seconds
    horizon = rules.min_start_distance_min * 60
    for _ in range(50):
        dodger = rng.randrange(tt.n_nodes)
        chosen: list[int] = []
        for _ in range(max_attempts):
            candidate = rng.randrange(tt.n_nodes)
            if candidate == dodger or (rules.distinct_inspector_starts and candidate in chosen):
                continue
            if horizon and reachable(tt, [candidate], start, horizon).arrival(dodger) is not None:
                continue
            chosen.append(candidate)
            if len(chosen) == rules.n_inspectors:
                return dodger, tuple(chosen)
    msg = "could not place the Inspectors far enough from the Dodger; lower min_start_distance_min"
    raise ValueError(msg)


class Game:
    def __init__(
        self,
        tt: Timetable,
        rules: RulesConfig,
        dodger_start: int,
        inspector_starts: tuple[int, ...],
        *,
        seed: int | None = None,
    ) -> None:
        if len(inspector_starts) != rules.n_inspectors:
            msg = f"expected {rules.n_inspectors} Inspector starts, got {len(inspector_starts)}"
            raise ValueError(msg)
        for node in (dodger_start, *inspector_starts):
            if not 0 <= node < tt.n_nodes:
                msg = f"unknown start node {node}"
                raise ValueError(msg)
        if rules.start_seconds < tt.window[0] or rules.end_seconds > tt.window[1]:
            msg = "the game's time span is outside the network's timetable window"
            raise ValueError(msg)
        self.tt = tt
        self.rules = rules
        self.seed = seed
        self.dodger_start = dodger_start
        self.inspector_starts = inspector_starts
        self.dodger_position = dodger_start
        self.inspector_positions = inspector_starts
        self.history: list[RoundRecord] = []
        self.status = GameStatus.RUNNING

    @classmethod
    def random(cls, tt: Timetable, rules: RulesConfig, seed: int) -> Game:
        dodger, inspectors = random_start(tt, rules, random.Random(seed))
        return cls(tt, rules, dodger, inspectors, seed=seed)

    # --- state ------------------------------------------------------------------------
    @property
    def rounds_played(self) -> int:
        return len(self.history)

    @property
    def round_number(self) -> int:
        """1-based number of the round to be played next."""
        return self.rounds_played + 1

    @property
    def window(self) -> tuple[int, int]:
        return self.rules.round_window(self.rounds_played)

    @property
    def catches(self) -> tuple[Catch, ...]:
        return tuple(c for r in self.history for c in r.catches)

    def dodger_view(self) -> DodgerView:
        visible = self.inspector_positions if self.rules.inspectors_visible else None
        return DodgerView(self.rules, self.round_number, self.window, self.dodger_position, visible)

    def inspector_view(self) -> InspectorView:
        last_reveal = None
        for record in self.history:
            if record.revealed_node is not None:
                last_reveal = (record.round_number, record.revealed_node)
        return InspectorView(
            rules=self.rules,
            round_number=self.round_number,
            window=self.window,
            positions=self.inspector_positions,
            last_reveal=last_reveal,
            mode_log=tuple(r.revealed_modes for r in self.history),
            position_history=tuple(
                tuple(j.destination for j in r.inspector_journeys) for r in self.history
            ),
        )

    # --- play ---------------------------------------------------------------------------
    def play_round(self, dodger: Journey, inspectors: tuple[Journey, ...]) -> RoundRecord:
        """Validate and resolve one simultaneous round."""
        if self.status is not GameStatus.RUNNING:
            msg = f"game is over ({self.status.value})"
            raise RuntimeError(msg)
        if len(inspectors) != self.rules.n_inspectors:
            msg = f"expected {self.rules.n_inspectors} Inspector journeys"
            raise ValueError(msg)
        window = self.window
        validate_journey(self.tt, dodger, self.dodger_position, window)
        for journey, position in zip(inspectors, self.inspector_positions, strict=True):
            validate_journey(self.tt, journey, position, window)

        number = self.round_number
        catches = find_catches(self.tt, self.rules, number, dodger, inspectors, window[1])
        record = RoundRecord(
            round_number=number,
            window=window,
            dodger_journey=dodger,
            inspector_journeys=inspectors,
            catches=catches,
            revealed_node=dodger.destination if self.rules.is_reveal_round(number) else None,
            revealed_modes=dodger.modes if self.rules.reveal_modes else None,
        )
        self.history.append(record)
        self.dodger_position = dodger.destination
        self.inspector_positions = tuple(j.destination for j in inspectors)
        if catches:
            self.status = GameStatus.INSPECTORS_WON
        elif self.rounds_played >= self.rules.rounds:
            self.status = GameStatus.DODGER_WON
        return record

    # --- serialisation ------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        """Full replay log, hidden information included."""
        return {
            "seed": self.seed,
            "feed_version": self.tt.feed_version,
            "service_date": self.tt.service_date,
            "rules": self.rules.model_dump(mode="json"),
            "dodger_start": self.dodger_start,
            "inspector_starts": list(self.inspector_starts),
            "status": self.status.value,
            "rounds": [
                {
                    "round": r.round_number,
                    "window": list(r.window),
                    "dodger": r.dodger_journey.to_json(),
                    "inspectors": [j.to_json() for j in r.inspector_journeys],
                    "catches": [
                        {
                            "inspector": c.inspector,
                            "kind": c.kind,
                            "node": c.node,
                            "time": c.time,
                            "trip": c.trip,
                        }
                        for c in r.catches
                    ],
                    "revealed_node": r.revealed_node,
                    "revealed_modes": (
                        None if r.revealed_modes is None else [m.value for m in r.revealed_modes]
                    ),
                }
                for r in self.history
            ],
        }

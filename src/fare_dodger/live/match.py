"""A match: rounds of the live game between the human's team and the bots, CS-style.

The first team to win more than half of ``regulation_rounds`` (6 of 10) takes the match.
The human plays ``start_role`` for the first half of regulation and the other side for the
second half. At a tie after regulation (5-5), overtime rounds alternate sides every round
until one team leads by two. Every round is played on a district, in a shuffled order that
repeats once all districts have been played.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.game import LiveGame, Role, Status
from fare_dodger.live.world import World


@dataclass(frozen=True, slots=True)
class District:
    name: str
    world: World


@dataclass(frozen=True, slots=True)
class RoundResult:
    number: int
    district: int
    role: Role
    won: bool
    text: str


def role_for(round_number: int, regulation: int, start: Role) -> Role:
    """The human's side in round ``round_number`` (1-based)."""
    half = regulation // 2
    if round_number <= half:
        return start
    if round_number <= regulation:
        return start.other
    # Overtime: round ``regulation + 1`` switches back from the second half, then alternates.
    return start if (round_number - regulation - 1) % 2 == 0 else start.other


def match_winner(you: int, bots: int, rounds: int, regulation: int) -> bool | None:
    """``True`` if the human's team has won, ``False`` if the bots have, ``None`` if it goes on."""
    if rounds <= regulation:
        target = regulation // 2 + 1
        if you >= target or bots >= target:
            return you > bots
        return None
    if abs(you - bots) >= 2:
        return you > bots
    return None


class Match:
    def __init__(self, districts: list[District], cfg: LiveConfig, seed: int) -> None:
        if not districts:
            msg = "a match needs at least one district"
            raise ValueError(msg)
        self.districts = districts
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.start_role = Role(cfg.start_role)
        self.order: list[int] = []
        self.results: list[RoundResult] = []
        self.game = self._new_game()

    @property
    def round_number(self) -> int:
        return len(self.results) + (0 if self.round_recorded else 1)

    @property
    def round_recorded(self) -> bool:
        return len(self.results) == self._game_number

    @property
    def score(self) -> tuple[int, int]:
        you = sum(r.won for r in self.results)
        return you, len(self.results) - you

    @property
    def winner(self) -> bool | None:
        you, bots = self.score
        return match_winner(you, bots, len(self.results), self.cfg.regulation_rounds)

    def _district_for(self, number: int) -> int:
        while len(self.order) < number:
            cycle = list(range(len(self.districts)))
            self.rng.shuffle(cycle)
            if self.order and len(cycle) > 1 and cycle[0] == self.order[-1]:
                cycle.append(cycle.pop(0))  # no district twice in a row
            self.order.extend(cycle)
        return self.order[number - 1]

    def _new_game(self) -> LiveGame:
        number = len(self.results) + 1
        self._game_number = number
        self.district = self._district_for(number)
        self.role = role_for(number, self.cfg.regulation_rounds, self.start_role)
        world = self.districts[self.district].world
        return LiveGame(world, self.cfg, self.rng.randrange(2**31), self.role)

    def update(self) -> None:
        """Record the current round once it is over."""
        game = self.game
        if game.status is Status.RUNNING or self.round_recorded:
            return
        won = game.player_won
        assert won is not None
        text = game.caught_by or (
            "You escaped!" if game.role is Role.DODGER else "The Dodger got away."
        )
        self.results.append(RoundResult(self._game_number, self.district, game.role, won, text))

    def next_round(self) -> bool:
        """Start the next round, if the current one is over and the match is not."""
        self.update()
        if not self.round_recorded or self.winner is not None:
            return False
        self.game = self._new_game()
        return True

    def view(self) -> dict[str, Any]:
        you, bots = self.score
        number = self.round_number
        regulation = self.cfg.regulation_rounds
        winner = self.winner
        next_role = None
        if self.round_recorded and winner is None:
            next_role = role_for(number + 1, regulation, self.start_role).value
        return {
            "round": number,
            "regulation": regulation,
            "overtime": number > regulation,
            "score": {"you": you, "bots": bots},
            "role": self.role.value,
            "district": self.district,
            "district_name": self.districts[self.district].name,
            "round_over": self.round_recorded,
            "next_role": next_role,
            "winner": None if winner is None else ("you" if winner else "bots"),
            "history": [{"won": r.won, "role": r.role.value} for r in self.results],
        }

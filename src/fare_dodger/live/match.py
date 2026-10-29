"""A match: rounds of the live game between two teams, CS-style.

Each team has ``size`` places (1v1, 2v2 or 3v3); places without a human are played by bots.
Every round one team runs (the Dodgers) and the other hunts, with ``hunter_extra`` more bots
on the hunting side. Every Dodger is worth a point: one that escapes scores for the runners,
one that is caught scores for the hunters (so a 2v2 round can end 1-1).

The first team to more than half of the regulation points (``regulation_rounds`` times ``size``;
2 of 3 or 4 of 6 in 1v1) takes the match. Team A plays its starting side for the first half of
regulation and the other side for the second half. At a tie after regulation, overtime
rounds alternate sides every round until one team leads by two points. Every round is played
on a district, in a shuffled order that repeats once all districts have been played.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from fare_dodger.config import LiveConfig
from fare_dodger.live.districts import District, DistrictPool
from fare_dodger.live.game import YOU, LiveGame, Role, Seat, Status

__all__ = ["District", "Match", "RoundResult", "Team", "match_winner", "role_for"]


@dataclass
class Team:
    name: str
    #: The human players: (id, name).
    players: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RoundResult:
    number: int
    district: int
    #: Team A's side in this round.
    role: Role
    #: Points scored by team A and team B.
    points: tuple[int, int]
    text: str


def role_for(round_number: int, regulation: int, start: Role) -> Role:
    """Team A's side in round ``round_number`` (1-based), starting as ``start``."""
    half = regulation // 2
    if round_number <= half:
        return start
    if round_number <= regulation:
        return start.other
    # Overtime: round ``regulation + 1`` switches back from the second half, then alternates.
    return start if (round_number - regulation - 1) % 2 == 0 else start.other


def match_winner(a: int, b: int, rounds: int, regulation: int, size: int = 1) -> bool | None:
    """``True`` if team A has won on points, ``False`` if team B has, ``None`` if it goes on."""
    if rounds <= regulation:
        target = regulation * size // 2 + 1
        if a >= target or b >= target:
            return a > b
        return None
    if abs(a - b) >= 2:
        return a > b
    return None


class Match:
    def __init__(
        self,
        districts: list[District] | DistrictPool,
        cfg: LiveConfig,
        seed: int,
        teams: list[Team] | None = None,
        size: int = 1,
        start: Role | None = None,
        *,
        begin: bool = True,
    ) -> None:
        """Without ``teams``, a single-player match: you against the bots, starting as
        ``cfg.start_role``. ``start`` is team A's side in the first round.

        With ``begin=False`` the first round is not set up yet: load
        :meth:`upcoming_district` first (e.g. in the background), then call :meth:`begin`.
        """
        if not districts:
            msg = "a match needs at least one district"
            raise ValueError(msg)
        self.districts = (
            districts if isinstance(districts, DistrictPool) else DistrictPool.of(districts)
        )
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.teams = teams or [Team("You", [(YOU, "You")]), Team("Bots")]
        self.size = max(size, *(len(t.players) for t in self.teams))
        self.start = start if start is not None else Role(cfg.start_role)
        self.order: list[int] = []
        self.results: list[RoundResult] = []
        self._game_number = 0
        self.district = self._district_for(1)
        if begin:
            self.begin()

    def begin(self) -> None:
        """Set up the first round."""
        self.game = self._new_game()

    def upcoming_district(self) -> int:
        """The district of the next round to set up (the first one before :meth:`begin`)."""
        return self._district_for(self._game_number + 1)

    @property
    def round_number(self) -> int:
        return len(self.results) + (0 if self.round_recorded else 1)

    @property
    def round_recorded(self) -> bool:
        return len(self.results) == self._game_number

    @property
    def score(self) -> tuple[int, int]:
        """Points of team A and team B."""
        return sum(r.points[0] for r in self.results), sum(r.points[1] for r in self.results)

    @property
    def winner(self) -> int | None:
        """The winning team's index, once there is one."""
        a, b = self.score
        won = match_winner(a, b, len(self.results), self.cfg.regulation_rounds, self.size)
        return None if won is None else (0 if won else 1)

    def team_of(self, pid: str) -> int:
        for i, team in enumerate(self.teams):
            if any(p == pid for p, _ in team.players):
                return i
        msg = f"{pid!r} is in neither team"
        raise KeyError(msg)

    def drop_player(self, pid: str) -> None:
        """A player left: from the next round on, a bot plays their seat."""
        for team in self.teams:
            team.players = [(p, name) for p, name in team.players if p != pid]

    def _district_for(self, number: int) -> int:
        while len(self.order) < number:
            cycle = self.districts.playable()
            self.rng.shuffle(cycle)
            if self.order and len(cycle) > 1 and cycle[0] == self.order[-1]:
                cycle.append(cycle.pop(0))  # no district twice in a row
            self.order.extend(cycle)
        return self.order[number - 1]

    def _seats(self, team: Team, label: str) -> list[Seat]:
        seats = [Seat(pid, name) for pid, name in team.players]
        seats += [Seat(None, f"{label} bot {k + 1}") for k in range(self.size - len(seats))]
        return seats

    def _new_game(self) -> LiveGame:
        number = len(self.results) + 1
        self._game_number = number
        self.district = self._district_for(number)
        self.role = role_for(number, self.cfg.regulation_rounds, self.start)
        runners_team = 0 if self.role is Role.DODGER else 1
        runners = self._seats(self.teams[runners_team], "Dodger")
        hunters = self._seats(self.teams[1 - runners_team], "Hunter")
        world = self.districts.get(self.district)
        return LiveGame(
            world, self.cfg, self.rng.randrange(2**31), runners=runners, hunters=hunters
        )

    def update(self) -> None:
        """Record the current round once it is over."""
        game = self.game
        if game.status is Status.RUNNING or self.round_recorded:
            return
        free, caught = game.result()
        points = (free, caught) if self.role is Role.DODGER else (caught, free)
        text = game.caught_by or ("Every Dodger escaped." if not caught else "")
        self.results.append(RoundResult(self._game_number, self.district, self.role, points, text))

    def next_round(self) -> bool:
        """Start the next round, if the current one is over and the match is not."""
        self.update()
        if not self.round_recorded or self.winner is not None:
            return False
        self.game = self._new_game()
        return True

    def view(self, pid: str | None = None) -> dict[str, Any]:
        """The match from one player's side ("you" is their team, "them" the other)."""
        mine = self.team_of(pid) if pid is not None else 0
        other = 1 - mine
        score = self.score
        number = self.round_number
        regulation = self.cfg.regulation_rounds
        winner = self.winner
        my_role = self.role if mine == 0 else self.role.other
        next_role = None
        if self.round_recorded and winner is None:
            team_a = role_for(number + 1, regulation, self.start)
            next_role = (team_a if mine == 0 else team_a.other).value
        return {
            "round": number,
            "regulation": regulation,
            "overtime": number > regulation,
            "size": self.size,
            "target": regulation * self.size // 2 + 1,
            "score": {"you": score[mine], "them": score[other]},
            "names": {"you": self.teams[mine].name, "them": self.teams[other].name},
            "role": my_role.value,
            "district": self.district,
            "district_name": self.districts.name(self.district),
            "round_over": self.round_recorded,
            "next_role": next_role,
            "winner": None if winner is None else ("you" if winner == mine else "them"),
            "history": [
                {
                    "you": r.points[mine],
                    "them": r.points[other],
                    "won": r.points[mine] > r.points[other],
                    "role": (r.role if mine == 0 else r.role.other).value,
                }
                for r in self.results
            ],
        }

"""Run many seeded bot-vs-bot games.

Game ``i`` of a run with base seed ``s`` uses seed ``s + i`` for start positions and both
bots, so any single game can be replayed in isolation. Results are sorted by seed, which
makes the output independent of the number of worker processes.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import asdict, dataclass
from typing import Any

from fare_dodger.bots import DODGER_BOTS, INSPECTOR_BOTS
from fare_dodger.bots.belief import BeliefInspectors
from fare_dodger.config import RulesConfig
from fare_dodger.game.engine import GameStatus
from fare_dodger.network.timetable import Timetable
from fare_dodger.sim.play import play_game


@dataclass(frozen=True, slots=True)
class GameResult:
    seed: int
    dodger_bot: str
    inspector_bot: str
    winner: str
    rounds_played: int
    #: Rounds completed without being caught.
    rounds_survived: int
    dodger_start: int
    inspector_starts: tuple[int, ...]
    dodger_path: tuple[int, ...]
    catch_round: int | None = None
    catch_kind: str | None = None
    catch_node: int | None = None
    catch_node_name: str | None = None
    catch_time: int | None = None
    catching_inspector: int | None = None
    #: Mean size of the Inspectors' belief set (belief bots only).
    mean_belief_size: float | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["inspector_starts"] = list(self.inspector_starts)
        data["dodger_path"] = list(self.dodger_path)
        return data


def run_one(
    tt: Timetable, rules: RulesConfig, dodger_bot: str, inspector_bot: str, seed: int
) -> GameResult:
    dodger = DODGER_BOTS[dodger_bot](tt, seed)
    inspectors = INSPECTOR_BOTS[inspector_bot](tt, seed)
    game = play_game(tt, rules, dodger, inspectors, seed)
    catch = game.catches[0] if game.catches else None
    belief = None
    if isinstance(inspectors, BeliefInspectors) and inspectors.belief.history:
        sizes = inspectors.belief.history
        belief = round(sum(sizes) / len(sizes), 2)
    path = (game.dodger_start, *(r.dodger_journey.destination for r in game.history))
    return GameResult(
        seed=seed,
        dodger_bot=dodger_bot,
        inspector_bot=inspector_bot,
        winner="dodger" if game.status is GameStatus.DODGER_WON else "inspectors",
        rounds_played=game.rounds_played,
        rounds_survived=game.rounds_played - (1 if catch else 0),
        dodger_start=game.dodger_start,
        inspector_starts=game.inspector_starts,
        dodger_path=path,
        catch_round=catch.round_number if catch else None,
        catch_kind=catch.kind if catch else None,
        catch_node=catch.node if catch else None,
        catch_node_name=tt.node_name[catch.node] if catch else None,
        catch_time=catch.time if catch else None,
        catching_inspector=catch.inspector if catch else None,
        mean_belief_size=belief,
    )


_WORKER: dict[str, Any] = {}


def _init_worker(tt: Timetable, rules: RulesConfig) -> None:
    _WORKER["tt"] = tt
    _WORKER["rules"] = rules


def _run_task(task: tuple[str, str, int]) -> GameResult:
    dodger_bot, inspector_bot, seed = task
    return run_one(_WORKER["tt"], _WORKER["rules"], dodger_bot, inspector_bot, seed)


def run_matchups(
    tt: Timetable,
    rules: RulesConfig,
    matchups: list[tuple[str, str]],
    games: int,
    base_seed: int,
    workers: int = 1,
) -> list[GameResult]:
    """Play ``games`` games per matchup; results ordered by matchup, then seed."""
    for dodger_bot, inspector_bot in matchups:
        if dodger_bot not in DODGER_BOTS:
            msg = f"unknown Dodger bot {dodger_bot!r}; choose from {sorted(DODGER_BOTS)}"
            raise ValueError(msg)
        if inspector_bot not in INSPECTOR_BOTS:
            msg = f"unknown Inspector bot {inspector_bot!r}; choose from {sorted(INSPECTOR_BOTS)}"
            raise ValueError(msg)
    tasks = [(d, i, base_seed + g) for d, i in matchups for g in range(games)]
    if workers <= 1:
        results = [run_one(tt, rules, d, i, s) for d, i, s in tasks]
    else:
        ctx = mp.get_context("spawn")  # same behaviour on Windows, macOS and Linux
        with ctx.Pool(workers, initializer=_init_worker, initargs=(tt, rules)) as pool:
            results = pool.map(_run_task, tasks, chunksize=max(1, len(tasks) // (workers * 8)))
    order = {m: k for k, m in enumerate(matchups)}
    return sorted(results, key=lambda r: (order[(r.dodger_bot, r.inspector_bot)], r.seed))

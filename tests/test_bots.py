from __future__ import annotations

import json

import pytest

from fare_dodger.bots import DODGER_BOTS, INSPECTOR_BOTS, BeliefInspectors, GreedyDodger
from fare_dodger.config import RulesConfig
from fare_dodger.game.engine import Game, GameStatus
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import reachable
from fare_dodger.sim.play import play_game

RULES = RulesConfig(n_inspectors=2, min_start_distance_min=4, rounds=8)


@pytest.mark.parametrize("dodger", sorted(DODGER_BOTS))
@pytest.mark.parametrize("inspectors", sorted(INSPECTOR_BOTS))
def test_every_matchup_plays_valid_deterministic_games(
    timetable: Timetable, dodger: str, inspectors: str
) -> None:
    logs = []
    for _ in range(2):
        game = play_game(
            timetable,
            RULES,
            DODGER_BOTS[dodger](timetable, 11),
            INSPECTOR_BOTS[inspectors](timetable, 11),
            seed=11,
        )
        assert game.status is not GameStatus.RUNNING
        logs.append(json.dumps(game.to_json(), sort_keys=True))
    assert logs[0] == logs[1]


@pytest.mark.parametrize("reveal_modes", [False, True])
@pytest.mark.parametrize("seed", range(6))
def test_belief_always_contains_the_dodger(
    timetable: Timetable, seed: int, reveal_modes: bool
) -> None:
    rules = RULES.model_copy(update={"reveal_modes": reveal_modes, "reveal_every": 3})
    game = Game.random(timetable, rules, seed)
    dodger = DODGER_BOTS["random"](timetable, seed)
    inspectors = BeliefInspectors(timetable, seed)
    while game.status is GameStatus.RUNNING:
        moves = inspectors.move(game.inspector_view())
        assert game.dodger_position in inspectors.belief.nodes
        game.play_round(dodger.move(game.dodger_view()), moves)


def test_mode_reveals_shrink_the_belief(timetable: Timetable) -> None:
    sizes = {}
    for reveal in (False, True):
        total = 0
        for seed in range(5):
            rules = RULES.model_copy(update={"reveal_modes": reveal, "reveal_every": 0})
            game = Game.random(timetable, rules, seed)
            dodger = DODGER_BOTS["random"](timetable, seed)
            inspectors = BeliefInspectors(timetable, seed)
            while game.status is GameStatus.RUNNING:
                moves = inspectors.move(game.inspector_view())
                game.play_round(dodger.move(game.dodger_view()), moves)
            total += sum(inspectors.belief.history)
        sizes[reveal] = total
    assert sizes[True] <= sizes[False]


def test_greedy_dodger_maximises_minimum_chaser_time(timetable: Timetable) -> None:
    rules = RulesConfig(n_inspectors=1, min_start_distance_min=0)
    (central,) = timetable.find_nodes("Central")
    (west,) = timetable.find_nodes("West")
    game = Game(timetable, rules, central, (west,))
    journey = GreedyDodger(timetable, 0).move(game.dodger_view())
    window = game.window
    options = reachable(timetable, [central], window[0], 600).reached()
    chaser = reachable(timetable, [west], window[0], 45 * 60).arrivals()
    best = max(chaser.get(n, 1 << 30) for n in options)
    assert chaser.get(journey.destination, 1 << 30) == best

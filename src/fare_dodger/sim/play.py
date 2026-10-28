"""Play one bot-vs-bot game."""

from __future__ import annotations

from fare_dodger.bots.base import DodgerBot, InspectorBot
from fare_dodger.config import RulesConfig
from fare_dodger.game.engine import Game, GameStatus
from fare_dodger.network.timetable import Timetable


def play_game(
    tt: Timetable,
    rules: RulesConfig,
    dodger: DodgerBot,
    inspectors: InspectorBot,
    seed: int,
) -> Game:
    """Run a full game; start positions come from ``seed``."""
    game = Game.random(tt, rules, seed)
    while game.status is GameStatus.RUNNING:
        dodger_move = dodger.move(game.dodger_view())
        inspector_moves = inspectors.move(game.inspector_view())
        game.play_round(dodger_move, inspector_moves)
    return game

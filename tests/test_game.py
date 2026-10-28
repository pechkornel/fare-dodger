from __future__ import annotations

import dataclasses
import json

import pytest

from fare_dodger.config import RulesConfig
from fare_dodger.game.engine import Game, GameStatus, random_start, vehicle_overlap
from fare_dodger.game.validate import InvalidJourneyError, journey_errors, validate_journey
from fare_dodger.modes import Mode
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.journey import Journey, RideLeg, WalkLeg
from fare_dodger.routing.raptor import reachable
from fare_dodger.timeutil import parse_hms

T0800 = parse_hms("08:00")
ROUND = (T0800, T0800 + 600)


def n(tt: Timetable, name: str) -> int:
    (idx,) = tt.find_nodes(name)
    return idx


def go(tt: Timetable, origin: str, dest: str, t: int = T0800) -> Journey:
    return reachable(tt, [n(tt, origin)], t, 600).journey(n(tt, dest))


def stay(tt: Timetable, where: str, t: int = T0800) -> Journey:
    return Journey(n(tt, where), t)


def ride_leg(j: Journey) -> RideLeg:
    leg = j.legs[0]
    assert isinstance(leg, RideLeg)
    return leg


# --- validation ------------------------------------------------------------------------------


def test_router_journeys_are_valid(timetable: Timetable) -> None:
    result = reachable(timetable, [n(timetable, "South")], T0800, 600)
    for node in result.reached():
        validate_journey(timetable, result.journey(node), n(timetable, "South"), ROUND)


def test_wrong_origin_and_late_arrival(timetable: Timetable) -> None:
    journey = go(timetable, "Central", "West")
    errors = journey_errors(timetable, journey, n(timetable, "East"), ROUND)
    assert any("player is at" in e for e in errors)
    late = journey_errors(timetable, journey, n(timetable, "Central"), (T0800, T0800 + 180))
    assert late == ["journey arrives after the round ends"]


def test_tampered_times_are_rejected(timetable: Timetable) -> None:
    journey = go(timetable, "Central", "West")
    leg = ride_leg(journey)
    faster = dataclasses.replace(journey, legs=(dataclasses.replace(leg, arr=leg.arr - 60),))
    with pytest.raises(InvalidJourneyError, match="arrival time"):
        validate_journey(timetable, faster, n(timetable, "Central"), ROUND)
    wrong_mode = dataclasses.replace(journey, legs=(dataclasses.replace(leg, mode=Mode.TRAM),))
    with pytest.raises(InvalidJourneyError, match="mode"):
        validate_journey(timetable, wrong_mode, n(timetable, "Central"), ROUND)
    ghost = dataclasses.replace(journey, legs=(dataclasses.replace(leg, trip=10_000),))
    with pytest.raises(InvalidJourneyError, match="unknown trip"):
        validate_journey(timetable, ghost, n(timetable, "Central"), ROUND)


def test_walking_rules(timetable: Timetable) -> None:
    market, park = n(timetable, "Market"), n(timetable, "Park")
    duration = timetable.walk_duration(market, park)
    assert duration is not None
    there = WalkLeg(market, park, T0800, T0800 + duration)
    back = WalkLeg(park, market, T0800 + duration, T0800 + 2 * duration)
    validate_journey(timetable, Journey(market, T0800, (there,)), market, ROUND)
    errors = journey_errors(timetable, Journey(market, T0800, (there, back)), market, ROUND)
    assert any("two walks" in e for e in errors)
    teleport = WalkLeg(market, n(timetable, "West"), T0800, T0800 + 60)
    errors = journey_errors(timetable, Journey(market, T0800, (teleport,)), market, ROUND)
    assert any("no footpath" in e for e in errors)
    sprint = WalkLeg(market, park, T0800, T0800 + 30)
    errors = journey_errors(timetable, Journey(market, T0800, (sprint,)), market, ROUND)
    assert any("walking takes" in e for e in errors)


def test_change_time_is_enforced(timetable: Timetable) -> None:
    # Metro West 08:00 -> Central 08:02, then tram 08:04 is fine (120 s) ...
    west = n(timetable, "West")
    journey = reachable(timetable, [west], T0800, 1200).journey(n(timetable, "Market"))
    assert [leg.mode for leg in journey.legs] == [Mode.METRO, Mode.TRAM]
    validate_journey(timetable, journey, west, (T0800, T0800 + 1200))
    # ... but with a 3-minute change time the same journey is impossible.
    strict = dataclasses.replace(timetable, node_change_s=[180] * timetable.n_nodes)
    with pytest.raises(InvalidJourneyError, match="boarding is possible"):
        validate_journey(strict, journey, west, (T0800, T0800 + 1200))


# --- catches ---------------------------------------------------------------------------------


def make_game(tt: Timetable, dodger: str, inspectors: list[str], **rules: object) -> Game:
    cfg = RulesConfig.model_validate({"n_inspectors": len(inspectors), **rules})
    return Game(tt, cfg, n(tt, dodger), tuple(n(tt, i) for i in inspectors))


def test_catch_at_same_stop(timetable: Timetable) -> None:
    game = make_game(timetable, "Central", ["West"])
    record = game.play_round(go(timetable, "Central", "West"), (stay(timetable, "West"),))
    (catch,) = record.catches
    assert catch.kind == "stop"
    assert catch.node == n(timetable, "West")
    assert game.status is GameStatus.INSPECTORS_WON
    with pytest.raises(RuntimeError, match="over"):
        game.play_round(stay(timetable, "West"), (stay(timetable, "West"),))


def test_catch_on_same_vehicle(timetable: Timetable) -> None:
    # Westbound M1 leaves East 08:00 (Central 08:02, West 08:04).
    dodger = go(timetable, "East", "West")
    inspector = go(timetable, "East", "Central")
    assert ride_leg(dodger).trip == ride_leg(inspector).trip
    game = make_game(timetable, "East", ["East"], catch_on_same_stop=False)
    (catch,) = game.play_round(dodger, (inspector,)).catches
    assert (catch.kind, catch.node, catch.time) == ("vehicle", n(timetable, "East"), T0800)


def test_same_trip_without_overlap_is_not_a_catch(timetable: Timetable) -> None:
    dodger = go(timetable, "East", "Central")
    inspector = reachable(timetable, [n(timetable, "Central")], T0800, 600).journey(
        n(timetable, "West")
    )
    assert ride_leg(dodger).trip == ride_leg(inspector).trip  # same train, consecutive hops
    assert vehicle_overlap(ride_leg(dodger), ride_leg(inspector)) is None
    game = make_game(timetable, "East", ["Central"])
    assert game.play_round(dodger, (inspector,)).catches == ()


def test_catch_rules_can_be_disabled(timetable: Timetable) -> None:
    dodger, inspector = go(timetable, "Central", "West"), go(timetable, "East", "West")
    both = make_game(timetable, "Central", ["East"]).play_round(dodger, (inspector,))
    assert [c.kind for c in both.catches] == ["vehicle", "stop"]  # same train, same end
    game = make_game(
        timetable, "Central", ["East"], catch_on_same_stop=False, catch_on_same_vehicle=False
    )
    assert game.play_round(dodger, (inspector,)).catches == ()


# --- reveals, views, end of game -----------------------------------------------------------


def play_stays(game: Game, tt: Timetable, dodger: str, inspector: str, rounds: int) -> None:
    for _ in range(rounds):
        start = game.window[0]
        game.play_round(stay(tt, dodger, start), (stay(tt, inspector, start),))


def test_reveals_and_inspector_view(timetable: Timetable) -> None:
    game = make_game(timetable, "Central", ["South"], reveal_modes=True)
    play_stays(game, timetable, "Central", "South", 2)
    assert game.inspector_view().last_reveal is None
    play_stays(game, timetable, "Central", "South", 1)
    view = game.inspector_view()
    assert view.last_reveal == (3, n(timetable, "Central"))
    assert view.mode_log == ((), (), ())
    assert view.position_history == ((n(timetable, "South"),),) * 3
    assert not hasattr(view, "dodger_position")


def test_modes_hidden_by_default(timetable: Timetable) -> None:
    game = make_game(timetable, "Central", ["South"])
    game.play_round(go(timetable, "Central", "West"), (stay(timetable, "South"),))
    assert game.inspector_view().mode_log == (None,)


def test_dodger_view(timetable: Timetable) -> None:
    game = make_game(timetable, "Central", ["South"])
    assert game.dodger_view().inspector_positions == (n(timetable, "South"),)
    hidden = make_game(timetable, "Central", ["South"], inspectors_visible=False)
    assert hidden.dodger_view().inspector_positions is None


def test_dodger_wins_after_last_round(timetable: Timetable) -> None:
    game = make_game(timetable, "Central", ["South"], rounds=4)
    play_stays(game, timetable, "Central", "South", 4)
    assert game.status is GameStatus.DODGER_WON
    assert game.window[0] == T0800 + 4 * 600
    json.dumps(game.to_json())


def test_rejects_bad_setup(timetable: Timetable) -> None:
    with pytest.raises(ValueError, match="Inspector starts"):
        Game(timetable, RulesConfig(), 0, (1,))
    with pytest.raises(ValueError, match="window"):
        Game(timetable, RulesConfig(n_inspectors=1, start_time="11:00"), 0, (1,))


def test_random_start_is_deterministic_and_far(timetable: Timetable) -> None:
    rules = RulesConfig(n_inspectors=2, min_start_distance_min=5)
    import random

    a = random_start(timetable, rules, random.Random(3))
    assert a == random_start(timetable, rules, random.Random(3))
    dodger, inspectors = a
    assert len(set(inspectors)) == 2
    for i in inspectors:
        assert reachable(timetable, [i], T0800, 300).arrival(dodger) is None
    with pytest.raises(ValueError, match="far enough"):
        random_start(
            timetable,
            RulesConfig(n_inspectors=2, min_start_distance_min=600),
            random.Random(0),
            max_attempts=20,
        )

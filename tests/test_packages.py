from __future__ import annotations

import pytest

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.game import LiveGame, Seat, Status
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def one_v_one(world: World) -> LiveGame:
    """Rob runs, Ann hunts (the human seated second, as in a 1v1 room), no bot hunters."""
    game = LiveGame(
        world,
        LiveConfig(inspected_share=0.0),
        seed=2,
        runners=[Seat("rob", "Rob")],
        hunters=[Seat("ann", "Ann")],
    )
    game.hunters.agents = []
    return game


def test_packages_wait_around_the_district(world: World) -> None:
    game = one_v_one(world)
    assert len(game.packages) == game.cfg.packages_waiting == 3
    for pkg in game.packages:
        assert world.contains(pkg.pos)
        assert all(distance(pkg.pos, q) >= 60 for q in world.stop_xy)


def test_accept_with_e_and_deliver_for_the_reward(world: World) -> None:
    game = one_v_one(world)
    rob = game.runners[0]
    pkg = game.packages[0]
    rob.traveller.pos = pkg.pos
    assert game.snapshot("rob")["package_here"] is True
    money = rob.traveller.money
    game.use("rob")
    assert rob.package is pkg and pkg.dest is not None
    assert game.cfg.package_min_m <= distance(pkg.pos, pkg.dest) <= game.cfg.package_max_m
    assert len(game.packages) == 3 and pkg not in game.packages  # a new one appeared
    delivery = game.snapshot("rob")["delivery"]
    assert delivery["reward"] == 1200 and delivery["near"]
    rob.traveller.pos = pkg.dest
    game.step(1)
    after = game.runners[0]  # re-read: mypy keeps the narrowing from before step()
    assert after.package is None and after.deliveries == 1
    assert after.traveller.money == money + 1200
    assert any("Delivered" in e.text for e in game.drain_events("rob"))


def test_the_bounty_hunters_never_see_packages(world: World) -> None:
    game = one_v_one(world)
    ann = game.snapshot("ann")
    assert ann["packages"] == [] and ann["delivery"] is None and ann["package_here"] is False
    assert len(game.snapshot("rob")["packages"]) == 3


def test_a_caught_dodger_loses_their_package(world: World) -> None:
    game = one_v_one(world)
    rob = game.runners[0]
    rob.traveller.pos = game.packages[0].pos
    game.use("rob")
    assert rob.package is not None
    game.humans["ann"].traveller.pos = rob.traveller.pos
    game.step(1)
    assert rob.caught and rob.package is None


def test_everyone_reads_their_own_result(world: World) -> None:
    # The bug: in a 1v1 the catcher, seated second, saw "The Dodger got away."
    game = one_v_one(world)
    game.humans["ann"].traveller.pos = game.runners[0].traveller.pos
    game.step(1)
    assert game.status is Status.CAUGHT
    assert game.snapshot("ann")["message"] == "You caught the Dodger!"
    assert game.snapshot("rob")["message"] == "A Bounty Hunter caught you."
    escaped = one_v_one(world)
    escaped.step(10_000)
    assert escaped.snapshot("ann")["message"] == "Time's up: the Dodger got away."
    assert escaped.snapshot("rob")["message"] == "You made it! The Bounty Hunters gave up."


def test_results_with_several_dodgers(world: World) -> None:
    game = LiveGame(
        world,
        LiveConfig(inspected_share=0.0),
        seed=2,
        runners=[Seat("rob", "Rob"), Seat(None, "Dodger bot 1")],
        hunters=[Seat("ann", "Ann"), Seat(None, "Hunter bot 1")],
    )
    game.hunters.agents = []
    for runner in game.runners:
        runner.bot = None
    game.humans["ann"].traveller.pos = game.runners[0].traveller.pos
    game.step(1)
    game.step(10_000)
    assert game.snapshot("ann")["message"] == "You caught Rob! Dodger bot 1 got away."
    assert game.snapshot("rob")["message"] == ("A Bounty Hunter caught you. Dodger bot 1 got away.")

from __future__ import annotations

import pytest

from fare_dodger.config import Area, LiveConfig, NetworkConfig
from fare_dodger.live.game import LiveGame, Seat, Status
from fare_dodger.live.landmarks import Landmark, landmark_kind, parse_landmarks, place_landmarks
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable
from fare_dodger.routing.raptor import reachable


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
    here = game.snapshot("rob")["package_here"]
    assert here is not None and here["near"] == pkg.near and here["reward"] == 1200
    money = rob.traveller.money
    game.use("rob")
    assert rob.package is pkg
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


def landmark_world(timetable: Timetable, area: Area) -> World:
    """A fresh world (the shared one stays without landmarks) with a statue at every
    station, so some are in range of every package."""
    world = World(timetable, area)
    world.landmarks = [
        Landmark(f"Statue {n}", "statue", world.node_xy[n]) for n in range(timetable.n_nodes)
    ]
    return world


def test_packages_go_to_landmarks_one_or_two_rides_away(
    timetable: Timetable, fixture_network_config: NetworkConfig
) -> None:
    world = landmark_world(timetable, fixture_network_config.area)
    game = one_v_one(world)
    names = [pkg.landmark for pkg in game.packages]
    assert all(name.startswith("Statue ") for name in names)
    assert len(set(names)) == len(names)  # never two packages to one landmark
    for pkg in game.packages:
        origin = world.nearest_node(pkg.pos)
        assert origin is not None
        rides = reachable(world.tt, [origin], int(game.t), 20 * 60, max_rounds=2).arrivals()
        assert world.nearest_node(pkg.dest) in rides
        assert game.cfg.package_min_m <= distance(pkg.pos, pkg.dest) <= game.cfg.package_max_m
    # Standing at a package shows where it goes before taking it.
    rob, pkg = game.runners[0], game.packages[0]
    rob.traveller.pos = pkg.pos
    here = game.snapshot("rob")["package_here"]
    assert here["landmark"] == pkg.landmark and here["kind"] == "statue"
    game.use("rob")
    assert any(pkg.landmark in e.text for e in game.drain_events("rob"))
    assert game.snapshot("rob")["delivery"]["landmark"] == pkg.landmark


def test_without_landmarks_packages_go_to_street_spots(world: World) -> None:
    game = one_v_one(world)
    assert world.landmarks == []
    assert all(pkg.landmark == "" and pkg.near for pkg in game.packages)


def test_landmark_kinds() -> None:
    assert landmark_kind({"historic": "memorial", "memorial": "statue"}) == "statue"
    assert landmark_kind({"historic": "memorial", "memorial": "war_memorial"}) == "monument"
    assert landmark_kind({"tourism": "artwork", "artwork_type": "fountain"}) == "fountain"
    assert landmark_kind({"tourism": "museum"}) == "museum"
    assert landmark_kind({"amenity": "theatre"}) == "theatre"
    assert landmark_kind({"historic": "castle"}) == "castle"
    # Too small or not a sight: plaques, stumbling stones, tombs, galleries.
    assert landmark_kind({"historic": "memorial", "memorial": "plaque"}) is None
    assert landmark_kind({"historic": "memorial", "memorial": "stolperstein"}) is None
    assert landmark_kind({"historic": "tomb"}) is None
    assert landmark_kind({"tourism": "gallery"}) is None


def test_parse_and_place_landmarks(
    timetable: Timetable, fixture_network_config: NetworkConfig
) -> None:
    world = World(timetable, fixture_network_config.area)
    lat, lon = timetable.node_lat[0], timetable.node_lon[0]

    def node(where: tuple[float, float] = (lat, lon), **tags: str) -> dict[str, object]:
        return {"type": "node", "lat": where[0], "lon": where[1], "tags": tags}

    museum = {"type": "way", "center": {"lat": lat, "lon": lon}}
    data = {
        "elements": [
            node(name="Hero", historic="memorial", memorial="statue"),
            {**museum, "tags": {"name": "Museum", "tourism": "museum"}},  # a building: its centre
            node(name="Hero", tourism="artwork", artwork_type="statue"),
            node(name="A plaque", historic="memorial", memorial="plaque"),
            node(historic="monument"),  # no name
            node((0.0, 0.0), name="Far", tourism="museum"),
        ]
    }
    parsed = parse_landmarks(data)
    assert [m["name"] for m in parsed] == ["Hero", "Museum", "Hero", "Far"]
    placed = place_landmarks([(m["name"], m["kind"], m["lat"], m["lon"]) for m in parsed], world)
    assert sorted(m.name for m in placed) == ["Hero", "Museum"]  # one per name, inside only


def test_the_bounty_hunters_never_see_packages(world: World) -> None:
    game = one_v_one(world)
    ann = game.snapshot("ann")
    assert ann["packages"] == [] and ann["delivery"] is None and ann["package_here"] is None
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

from __future__ import annotations

import itertools
import math

import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.game import HEADING_M, Keys, LiveGame
from fare_dodger.live.match import District
from fare_dodger.live.server import apply_message, create_app
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def node(world: World, name: str) -> int:
    return world.tt.find_nodes(name)[0]


def solo(world: World, at: str) -> LiveGame:
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=1)
    game.dodger.pos = world.node_xy[node(world, at)]
    game.hunters.agents = []
    return game


# --- sprint --------------------------------------------------------------------------------


def test_sprinting_is_faster_until_the_stamina_runs_out(world: World) -> None:
    game = solo(world, "Central")
    cfg, p = game.cfg, game.dodger
    x0 = p.pos[0]
    game.keys = Keys(right=True, sprint=True)
    game.step(10)
    assert p.pos[0] - x0 == pytest.approx(10 * cfg.walk_speed_mps * cfg.sprint_factor)
    assert p.sprinting and p.stamina == pytest.approx(1 - 10 / cfg.sprint_s)
    game.step(cfg.sprint_s)  # spent: back to jogging
    p = game.dodger  # re-read: mypy keeps the narrowing from before step()
    assert p.stamina < 0.25 and not p.sprinting
    x1 = p.pos[0]
    game.step(10)
    assert p.pos[0] - x1 == pytest.approx(10 * cfg.walk_speed_mps)
    # Still holding Shift, it needs a quarter of the stamina back before sprinting again.
    p.stamina = 0.2
    game.step(1)
    assert not game.dodger.sprinting
    p.stamina = 0.25
    game.step(1)
    assert game.dodger.sprinting


def test_stamina_refills_and_is_not_spent_standing_still(world: World) -> None:
    game = solo(world, "Central")
    p = game.dodger
    p.stamina = 0.5
    game.keys = Keys(sprint=True)  # Shift alone does not sprint
    game.step(game.cfg.sprint_recover_s * 0.25)
    assert not p.sprinting and p.stamina == pytest.approx(0.75)
    game.step(game.cfg.sprint_recover_s)
    assert p.stamina == 1
    snap = game.snapshot()["player"]
    assert snap["stamina"] == 1 and snap["sprinting"] is False


def test_the_keys_message_carries_shift(world: World) -> None:
    game = solo(world, "Central")
    apply_message(game, {"type": "keys", "right": True, "sprint": True})
    assert game.keys.sprint and game.keys.right


# --- which way a vehicle goes --------------------------------------------------------------


def test_departures_say_which_way_they_go(world: World) -> None:
    game = solo(world, "West")
    deps = game.snapshot()["stop"]["departures"]
    assert deps
    first = deps[0]
    assert first["headsign"] == "East" and first["via"][0] == "Central"
    assert "West" not in first["via"]
    # The bearing points from West towards the stop ahead.
    (x0, y0), (x1, y1) = world.node_xy[node(world, "West")], world.node_xy[node(world, "Central")]
    expected = math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360
    assert abs((first["bearing"] - expected + 180) % 360 - 180) <= 1


def test_the_line_ahead_is_cut_to_length(world: World) -> None:
    dep = world.departures(node(world, "West"), game_t(world), 600, 1)[0]
    path = world.path_ahead(dep.pattern, dep.pos, 0.0, HEADING_M)
    assert len(path) >= 2
    assert sum(distance(a, b) for a, b in itertools.pairwise(path)) == pytest.approx(HEADING_M)
    full = world.path_ahead(dep.pattern, dep.pos, 0.0, 1e9)
    assert full[-1] == world.stop_point(dep.pattern, len(world.tt.pattern_stops(dep.pattern)) - 1)


def test_a_ride_shows_its_next_stops(world: World) -> None:
    game = solo(world, "West")
    game.wait_for(0)
    game.step(1)
    ride = game.snapshot()["ride"]
    assert ride["next_stop"] == "Central" and ride["via"] == ["East"]  # after the next
    assert ride["bearing"] is not None


def test_the_server_gives_the_line_ahead(world: World) -> None:
    dep = world.departures(node(world, "West"), game_t(world), 600, 1)[0]
    with TestClient(create_app([District("X", world)], LiveConfig(seed=1))) as client:
        client.get("/api/world/0")
        body = client.get(f"/api/ahead/0/{dep.pattern}/{dep.pos}").json()
        assert len(body["path"]) >= 2
        last = len(world.tt.pattern_stops(dep.pattern)) - 1
        assert client.get(f"/api/ahead/0/{dep.pattern}/{last}").status_code == 404
        assert client.get("/api/ahead/5/0/0").status_code == 404


def game_t(world: World) -> float:
    return LiveGame(world, LiveConfig(), seed=1).t

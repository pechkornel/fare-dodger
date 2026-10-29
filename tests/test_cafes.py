from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.cafes import (
    Cafe,
    download_cafes,
    overpass_query,
    pick_cafes,
    read_cafes,
)
from fare_dodger.live.game import Keys, LiveGame, Role, Status, world_view
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable


@pytest.fixture
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def coffee_game(world: World, **overrides: object) -> LiveGame:
    """A game with the Dodger standing at a café at Central and nobody else around."""
    central = world.node_xy[world.tt.find_nodes("Central")[0]]
    world.cafes = [Cafe("Kávé", central)]
    cfg = LiveConfig.model_validate({"inspected_share": 0.0, **overrides})
    game = LiveGame(world, cfg, seed=1)
    game.hunters.agents = []
    game.dodger.pos = central
    return game


def test_coffee_costs_money_takes_time_then_makes_you_fast(world: World) -> None:
    game = coffee_game(world)
    cfg, d = game.cfg, game.dodger
    start = d.pos
    game.buy_coffee()
    assert d.money == cfg.start_money_ft - cfg.coffee_price_ft
    assert game.snapshot()["coffee"]["brewing_s"] == cfg.coffee_brew_s
    game.keys = Keys(right=True)
    game.step(cfg.coffee_brew_s - 1)
    assert d.pos == start  # standing at the counter
    game.wait_for(0)
    assert d.waiting_for is None
    game.step(1)
    assert d.brewing_until is None and d.boosted_until is not None and d.coffees == 1
    x0 = d.pos[0]
    game.step(10)
    assert d.pos[0] - x0 == pytest.approx(10 * cfg.coffee_speed_mps)
    game.step(cfg.coffee_boost_s)
    after = game.dodger  # re-read: mypy keeps the narrowing from before step()
    assert after.boosted_until is None
    x1 = d.pos[0]
    game.step(10)
    assert d.pos[0] - x1 == pytest.approx(10 * cfg.walk_speed_mps)
    assert game.status is Status.RUNNING


def test_no_coffee_away_from_a_cafe_without_money_or_as_a_hunter(world: World) -> None:
    game = coffee_game(world)
    game.dodger.pos = (game.dodger.pos[0] + 500, game.dodger.pos[1])
    game.buy_coffee()
    assert game.dodger.money == game.cfg.start_money_ft and game.dodger.brewing_until is None
    broke = coffee_game(world, start_money_ft=100)
    broke.buy_coffee()
    assert broke.dodger.money == 100 and broke.dodger.brewing_until is None
    hunter = LiveGame(world, LiveConfig(), seed=1, role=Role.HUNTER)
    hunter.player.pos = world.cafes[0].pos
    hunter.buy_coffee()
    assert hunter.player.brewing_until is None
    assert any("duty" in e.text for e in hunter.drain_events())


def test_cafes_are_in_the_world_view_and_snapshot(world: World) -> None:
    game = coffee_game(world)
    view = world_view(world, game.cfg)
    assert [c["name"] for c in view["cafes"]] == ["Kávé"]
    assert view["coffee_price_ft"] == 500
    assert game.snapshot()["coffee"] == {"cafe": "Kávé", "brewing_s": None, "boost_s": None}


def test_pick_cafes_spreads_them_inside_the_area(world: World) -> None:
    lat0, lon0 = world.proj.lat0, world.proj.lon0
    cafes = [
        ("Middle", lat0, lon0),
        ("Next door", lat0 + 0.0002, lon0),
        ("North", lat0 + 0.02, lon0),
        ("South", lat0 - 0.02, lon0),
        ("Far away", lat0 + 1.0, lon0),  # outside the fixture's 5 km circle
    ]
    picked = pick_cafes(cafes, world, 3)
    assert picked[0].name == "Middle"
    assert {c.name for c in picked} == {"Middle", "North", "South"}
    assert pick_cafes(cafes, world, 0) == []
    assert all(math.isfinite(c.pos[0]) for c in picked)


def test_download_and_read_cafes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "amenity" in httpx.QueryParams(request.content.decode())["data"]
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "node", "id": 1, "lat": 47.5, "lon": 19.05, "tags": {"name": "Ó"}},
                    {"type": "node", "id": 2, "lat": 47.51, "lon": 19.06},
                ]
            },
        )

    bbox = (47.49, 19.04, 47.52, 19.07)
    path = download_cafes(
        tmp_path / "cafes.json", bbox, httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert read_cafes(path, bbox) == [("Ó", 47.5, 19.05), ("Café", 47.51, 19.06)]
    assert read_cafes(path, (47.0, 19.0, 47.6, 19.1)) is None  # not covered
    assert read_cafes(tmp_path / "missing.json") is None
    assert "cafe" in overpass_query(bbox)


def test_a_coffee_tells_the_bounty_hunters_which_cafe(world: World) -> None:
    # Camping in cafés was too strong: buying a coffee reports the exact café.
    game = coffee_game(world)
    d = game.dodger
    game.buy_coffee()
    ping = game.pings[-1]
    assert ping.pos == d.pos and ping.until == d.brewing_until
    assert game.hunters.last_ping == ping  # the bots head there
    assert any("barista reported you at Kávé" in e.text for e in game.drain_events())

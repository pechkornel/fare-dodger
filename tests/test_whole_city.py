from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.districts import DistrictPool, DistrictSpec
from fare_dodger.live.game import Keys, LiveGame
from fare_dodger.live.match import Match
from fare_dodger.live.server import Limits, create_app
from fare_dodger.live.water import WaterMask, download_water, overpass_query, parse, read_water
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable


def square(x0: float, y0: float, size: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size), (x0, y0)]


def geom(points: list[tuple[float, float]]) -> list[dict[str, float]]:
    return [{"lat": lat, "lon": lon} for lat, lon in points]


# --- water ---------------------------------------------------------------------------------


def test_water_areas_islands_and_bridges_from_overpass() -> None:
    river = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    island = [(0.4, 0.4), (0.4, 0.6), (0.6, 0.6), (0.6, 0.4), (0.4, 0.4)]
    elements = [
        {
            "type": "relation",
            "tags": {"natural": "water"},
            "members": [
                # The outer ring comes in two halves that must be stitched.
                {"type": "way", "role": "outer", "geometry": geom(river[:3])},
                {"type": "way", "role": "outer", "geometry": geom(river[2:])},
                {"type": "way", "role": "inner", "geometry": geom(island)},
            ],
        },
        {"type": "way", "tags": {"natural": "water"}, "geometry": geom(island)},  # a pond
        {
            "type": "way",
            "tags": {"highway": "primary", "bridge": "yes"},
            "geometry": geom(river[:2]),
        },
    ]
    data = parse(elements)
    assert len(data["areas"]) == 2
    assert len(data["areas"][0]["outer"]) == 1 and len(data["areas"][0]["inner"]) == 1
    assert data["bridges"] == [river[:2]]


def test_the_mask_keeps_islands_dry_and_bridges_open() -> None:
    river = square(0, 0, 200)
    island = square(80, 80, 40)
    bridge = [(-10.0, 30.0), (210.0, 30.0)]  # east-west across the river at y = 30
    mask = WaterMask([([river], [island])], [bridge], (-50, -50, 250, 250))
    assert mask.blocks((50, 150)) and mask.blocks((150, 60))
    assert not mask.blocks((100, 100))  # the island
    assert not mask.blocks((100, 30)) and not mask.blocks((100, 36))  # on the bridge
    assert mask.blocks((100, 50))  # beside it
    assert not mask.blocks((-30, 100)) and not mask.blocks((1000, 1000))  # the bank, off the grid


def test_download_and_read_water(tmp_path: Path) -> None:
    bbox = (47.49, 19.03, 47.51, 19.06)
    pond = [(47.5, 19.04), (47.5, 19.05), (47.505, 19.05), (47.5, 19.04)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "elements": [{"type": "way", "tags": {"natural": "water"}, "geometry": geom(pond)}]
            },
        )

    path = download_water(
        tmp_path / "water.json", bbox, httpx.Client(transport=httpx.MockTransport(handler))
    )
    data = read_water(path, bbox)
    assert data is not None and len(data["areas"]) == 1
    assert read_water(path, (47.0, 19.0, 47.6, 19.1)) is None  # does not cover it
    assert json.loads(path.read_text())["version"] == 1
    assert '"bridge"' in overpass_query(bbox) and "riverbank" in overpass_query(bbox)


@pytest.fixture
def riverside(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    """Central, with a river 30 m east of it, crossed by a bridge 100 m to the north."""
    world = World(timetable, fixture_network_config.area)
    cx, cy = world.node_xy[world.tt.find_nodes("Central")[0]]
    river = [(cx + 30, cy - 500), (cx + 130, cy - 500), (cx + 130, cy + 500), (cx + 30, cy + 500)]
    bridge = [(cx, cy + 100), (cx + 200, cy + 100)]
    world.water = WaterMask(
        [([[*river, river[0]]], [])], [bridge], (cx - 600, cy - 600, cx + 600, cy + 600)
    )
    return world


def walker(world: World, pos: tuple[float, float]) -> LiveGame:
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=1)
    game.hunters.agents = []
    game.dodger.pos = pos
    return game


def test_the_river_stops_walkers_but_the_bridge_crosses_it(riverside: World) -> None:
    water = riverside.water
    assert water is not None
    cx, cy = riverside.node_xy[riverside.tt.find_nodes("Central")[0]]
    game = walker(riverside, (cx, cy))
    game.keys = Keys(right=True)
    game.step(40)
    assert game.dodger.pos[0] < cx + 30 and not water.blocks(game.dodger.pos)
    game = walker(riverside, (cx, cy + 100))
    game.keys = Keys(right=True)
    game.step(160 / game.cfg.walk_speed_mps)
    assert game.dodger.pos[0] > cx + 130  # over the bridge to the other bank
    game = walker(riverside, (cx + 80, cy - 200))  # somehow in the river: can still get out
    game.keys = Keys(left=True)
    game.step(40)
    assert not water.blocks(game.dodger.pos)


def test_random_spots_are_never_in_the_water(riverside: World) -> None:
    import random

    rng = random.Random(2)
    water = riverside.water
    assert water is not None
    spots = [riverside.random_spot(rng) for _ in range(200)]
    assert not any(water.blocks(p) for p in spots if p is not None)


# --- districts loaded on demand ------------------------------------------------------------


@pytest.fixture(scope="module")
def world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    return World(timetable, fixture_network_config.area)


def test_district_slugs() -> None:
    net = NetworkConfig()
    assert DistrictSpec("Lánchíd: Víziváros & Belváros", net).slug == "lanchid-vizivaros-belvaros"


def counting_pool(world: World, n: int = 4, keep: int = 2) -> tuple[DistrictPool, list[int]]:
    loads: list[int] = []

    def load(i: int) -> World:
        loads.append(i)
        return world

    return DistrictPool([f"D{i}" for i in range(n)], load, keep=keep), loads


def test_the_pool_loads_lazily_and_forgets_the_oldest(world: World) -> None:
    pool, loads = counting_pool(world)
    assert len(pool) == 4 and pool.loaded() == [] and not pool.ready(0)
    pool.get(0)
    pool.get(0)
    pool.get(1)
    assert loads == [0, 1] and pool.ready(1)
    pool.get(0)  # 0 is now the most recent, so 1 goes
    pool.get(2)
    assert sorted(pool.loaded()) == [0, 2] and loads == [0, 1, 2]


def test_preparing_loads_once_however_many_ask(world: World) -> None:
    pool, loads = counting_pool(world)

    async def three_rooms() -> list[World]:
        return list(await asyncio.gather(pool.prepare(3), pool.prepare(3), pool.prepare(3)))

    assert asyncio.run(three_rooms()) == [world, world, world]
    assert loads == [3]


def test_a_match_names_its_district_before_it_begins(world: World) -> None:
    pool, loads = counting_pool(world, n=6, keep=6)
    match = Match(pool, LiveConfig(), seed=3, begin=False)
    first = match.upcoming_district()
    assert first == match.district and loads == []
    match.begin()
    assert loads == [first]
    upcoming = match.upcoming_district()
    pool.get(upcoming)  # what a room does while the round is played
    match.game.status = match.game.status.ESCAPED
    match.update()
    assert match.next_round() and match.district == upcoming and loads == [first, upcoming]


def until(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        msg: dict[str, Any] = ws.receive_json()
        if msg["type"] == kind:
            return msg


def test_the_server_loads_a_district_when_a_match_needs_it(world: World) -> None:
    pool, loads = counting_pool(world, n=3, keep=3)
    with TestClient(create_app(pool, LiveConfig(seed=5), Limits())) as client:
        assert loads == []  # nothing is built up front
        assert client.get("/api/backdrop").json() == {"district": 0}
        assert client.get("/api/world/2").json()["name"] == "D2"
        assert loads == [2]
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "solo", "role": "dodger"})
            first = until(ws, "snapshot")
            assert first["match"]["round"] == 1
            # The next round's district loads (in the background) while round 1 is played.
            ws.send_json({"type": "keys"})
            until(ws, "snapshot")
            assert len(set(loads)) >= 2

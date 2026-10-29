from __future__ import annotations

import math
from pathlib import Path

import httpx
import pytest

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.buildings import (
    Buildings,
    download_buildings,
    footprints,
    overpass_query,
    read_buildings,
)
from fare_dodger.live.game import Keys, LiveGame
from fare_dodger.live.world import World, distance
from fare_dodger.network.timetable import Timetable


def square(x0: float, y0: float, size: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size), (x0, y0)]


def test_keys_follow_the_screen_however_the_map_is_turned() -> None:
    assert Keys(up=True).direction() == pytest.approx((0.0, 1.0))
    # Map turned 90 degrees clockwise: east is at the top of the screen.
    assert Keys(up=True, bearing=90).direction() == pytest.approx((1.0, 0.0))
    assert Keys(right=True, bearing=90).direction() == pytest.approx((0.0, -1.0))
    diagonal = Keys(up=True, right=True).direction()
    assert diagonal == pytest.approx((math.sqrt(0.5), math.sqrt(0.5)))
    assert Keys(up=True, down=True).direction() == (0.0, 0.0)


def test_footprints_from_overpass() -> None:
    ring = [
        {"lat": 47.5, "lon": 19.05},
        {"lat": 47.5, "lon": 19.051},
        {"lat": 47.501, "lon": 19.051},
        {"lat": 47.5, "lon": 19.05},
    ]
    half_a, half_b = ring[:3], ring[2:]  # one outer ring split into two open ways
    elements = [
        {"type": "way", "tags": {"building": "yes"}, "geometry": ring},
        {"type": "way", "tags": {"building": "roof"}, "geometry": ring},  # a canopy
        {"type": "way", "tags": {"building": "yes", "layer": "-1"}, "geometry": ring},
        {
            "type": "relation",
            "tags": {"building": "yes"},
            "members": [
                {"type": "way", "role": "outer", "geometry": half_a},
                {"type": "way", "role": "outer", "geometry": half_b},
                {"type": "way", "role": "inner", "geometry": ring},
            ],
        },
    ]
    rings = footprints(elements)
    assert len(rings) == 2 and all(r[0] == r[-1] for r in rings)


def test_buildings_block_except_around_stops() -> None:
    buildings = Buildings([square(0, 0, 20), square(100, 0, 20)], keep_open=[(110, 10)])
    assert len(buildings) == 1  # the one holding a stop stays open
    assert buildings.at((10, 10)) == 0 and buildings.at((110, 10)) is None
    assert buildings.at((50, 50)) is None


def test_download_and_read_buildings(tmp_path: Path) -> None:
    ring = [
        {"lat": 47.5, "lon": 19.05},
        {"lat": 47.5, "lon": 19.051},
        {"lat": 47.501, "lon": 19.051},
        {"lat": 47.5, "lon": 19.05},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "building" in httpx.QueryParams(request.content.decode())["data"]
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "way", "tags": {"building": "house"}, "geometry": ring},
                ]
            },
        )

    bbox = (47.49, 19.04, 47.52, 19.07)
    path = download_buildings(
        tmp_path / "buildings.json", bbox, httpx.Client(transport=httpx.MockTransport(handler))
    )
    polygons = read_buildings(path, bbox)
    assert polygons is not None and polygons[0][0] == (47.5, 19.05)
    assert read_buildings(path, (47.0, 19.0, 47.6, 19.1)) is None
    assert "building" in overpass_query(bbox)


@pytest.fixture
def walled(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    """Central, with a 40 m wide building whose west wall stands 20 m east of it."""
    world = World(timetable, fixture_network_config.area)
    cx, cy = world.node_xy[world.tt.find_nodes("Central")[0]]
    world.buildings = Buildings([square(cx + 20, cy - 20, 40)])
    return world


def walker(world: World) -> LiveGame:
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=1)
    game.hunters.agents = []
    game.dodger.pos = world.node_xy[world.tt.find_nodes("Central")[0]]
    return game


def test_walking_anywhere_but_through_a_building(walled: World) -> None:
    cx, cy = walled.node_xy[walled.tt.find_nodes("Central")[0]]
    game = walker(walled)
    game.keys = Keys(left=True, up=True)  # away from the building: free, diagonally
    game.step(10)
    x, y = game.dodger.pos
    assert x < cx and y > cy
    assert distance((cx, cy), game.dodger.pos) == pytest.approx(10 * game.cfg.walk_speed_mps)
    game = walker(walled)
    game.keys = Keys(right=True)  # straight at the wall
    game.step(30)
    assert game.dodger.pos[0] < cx + 20  # never inside
    assert walled.buildings is not None and walled.buildings.at(game.dodger.pos) is None


def test_sliding_along_a_wall(walled: World) -> None:
    cx, cy = walled.node_xy[walled.tt.find_nodes("Central")[0]]
    game = walker(walled)
    game.dodger.pos = (cx + 15, cy)
    # Up and to the right, into the wall at an angle: slides north along it.
    game.keys = Keys(right=True, up=True)
    game.step(10)
    x, y = game.dodger.pos
    assert x < cx + 20 and y > cy + 10


def test_walking_out_of_a_building_you_start_in(walled: World) -> None:
    cx, cy = walled.node_xy[walled.tt.find_nodes("Central")[0]]
    game = walker(walled)
    game.dodger.pos = (cx + 25, cy)  # just inside the west wall
    game.keys = Keys(left=True)
    game.step(5)
    assert game.dodger.pos[0] < cx + 20

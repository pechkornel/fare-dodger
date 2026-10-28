from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fare_dodger.config import LiveConfig, NetworkConfig
from fare_dodger.live.game import Keys, LiveGame
from fare_dodger.live.parks import Parks, download_parks, overpass_query, read_parks
from fare_dodger.live.streets import StreetGraph
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable


def square(x0: float, y0: float, size: float) -> list[tuple[float, float]]:
    return [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size), (x0, y0)]


def test_parks_contain_points_and_drop_tiny_shapes() -> None:
    parks = Parks([square(0, 0, 100), square(500, 500, 5)])  # the second is 25 m2
    assert len(parks) == 1
    assert parks.contains((50, 50)) and not parks.contains((150, 50))
    assert not parks.contains((502, 502))


@pytest.fixture
def park_world(timetable: Timetable, fixture_network_config: NetworkConfig) -> World:
    """Central with a street running east-west through it and a 200 m park north of it."""
    world = World(timetable, fixture_network_config.area)
    cx, cy = world.node_xy[world.tt.find_nodes("Central")[0]]
    world.set_streets(StreetGraph([(cx - 400, cy), (cx, cy), (cx + 400, cy)], [(0, 1), (1, 2)]))
    world.parks = Parks([square(cx - 100, cy + 5, 200)])
    return world


def game_at(world: World, dx: float, dy: float) -> LiveGame:
    cx, cy = world.node_xy[world.tt.find_nodes("Central")[0]]
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=1)
    game.hunters.agents = []
    game.dodger.pos = (cx + dx, cy + dy)
    return game


def test_walking_straight_across_a_park(park_world: World) -> None:
    cx, cy = park_world.node_xy[park_world.tt.find_nodes("Central")[0]]
    game = game_at(park_world, 0, 0)
    game.keys = Keys(up=True, right=True)  # diagonal, off the street into the park
    game.step(20)
    x, y = game.dodger.pos
    assert x > cx + 10 and y > cy + 10  # left the street, across the grass
    assert park_world.parks is not None and park_world.parks.contains(game.dodger.pos)


def test_leaving_a_park_steps_back_onto_the_street(park_world: World) -> None:
    cy = park_world.node_xy[park_world.tt.find_nodes("Central")[0]][1]
    game = game_at(park_world, 30, 40)
    game.keys = Keys(down=True)
    game.step(30)
    y = game.dodger.pos[1]
    assert y == pytest.approx(cy)  # back on the street (the park ends 5 m above it)


def test_download_and_read_parks(tmp_path: Path) -> None:
    ring = [
        {"lat": 47.5, "lon": 19.05},
        {"lat": 47.5, "lon": 19.06},
        {"lat": 47.51, "lon": 19.06},
        {"lat": 47.5, "lon": 19.05},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "leisure" in httpx.QueryParams(request.content.decode())["data"]
        return httpx.Response(
            200,
            json={
                "elements": [
                    {"type": "way", "id": 1, "geometry": ring},
                    {"type": "way", "id": 2, "geometry": ring[:3]},  # not closed: skipped
                    {
                        "type": "relation",
                        "id": 3,
                        "members": [
                            {"type": "way", "role": "outer", "geometry": ring},
                            {"type": "way", "role": "inner", "geometry": ring},
                        ],
                    },
                ]
            },
        )

    bbox = (47.49, 19.04, 47.52, 19.07)
    path = download_parks(
        tmp_path / "parks.json", bbox, httpx.Client(transport=httpx.MockTransport(handler))
    )
    polygons = read_parks(path, bbox)
    assert polygons is not None and len(polygons) == 2
    assert polygons[0][0] == (47.5, 19.05)
    assert read_parks(path, (47.0, 19.0, 47.6, 19.1)) is None
    assert json.loads(path.read_text())["version"] == 1
    assert "leisure" in overpass_query(bbox)

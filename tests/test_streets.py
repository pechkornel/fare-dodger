from __future__ import annotations

import itertools
import json
import math
from pathlib import Path

import httpx
import pytest

from fare_dodger.config import LiveConfig, NetworkConfig, PolygonArea
from fare_dodger.live.game import Keys, LiveGame, Status
from fare_dodger.live.streets import (
    STREETS_VERSION,
    EdgePos,
    RouteFollower,
    StreetGraph,
    along,
    area_bbox,
    download_streets,
    overpass_query,
    read_streets,
)
from fare_dodger.live.world import World
from fare_dodger.network.timetable import Timetable

EAST, NORTH, WEST, SOUTH = (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)


@pytest.fixture
def grid() -> StreetGraph:
    """A 3x3 grid of junctions 100 m apart (node = 3 * row + col), plus a bend:
    a dead-end street from the top-right corner going east 50 m and then north 50 m."""
    xy = [(100.0 * c, 100.0 * r) for r in range(3) for c in range(3)]
    edges = []
    for r in range(3):
        for c in range(3):
            n = 3 * r + c
            if c < 2:
                edges.append((n, n + 1))
            if r < 2:
                edges.append((n, n + 3))
    xy += [(250.0, 200.0), (250.0, 250.0)]
    edges += [(8, 9), (9, 10)]
    return StreetGraph(xy, edges)


def test_move_follows_the_street_and_turns_at_junctions(grid: StreetGraph) -> None:
    start = grid.snap((0.0, 0.0))
    ep = grid.move(start, EAST, 150.0)
    assert grid.point(ep) == pytest.approx((150.0, 0.0))
    # North between junctions: go back or on to the nearer matching junction and turn there.
    ep = grid.move(ep, NORTH, 100.0)
    x, y = grid.point(ep)
    assert x in (pytest.approx(100.0), pytest.approx(200.0))
    assert y == pytest.approx(50.0)


def test_diagonal_input_never_cuts_through_a_block(grid: StreetGraph) -> None:
    ep = grid.snap((0.0, 0.0))
    diagonal = (math.sqrt(0.5), math.sqrt(0.5))
    for _ in range(40):
        ep = grid.move(ep, diagonal, 10.0)
        x, y = grid.point(ep)
        on_street = min(abs(x - round(x / 100) * 100), abs(y - round(y / 100) * 100)) < 1e-6
        assert on_street or (x >= 200 and y >= 200)  # the bend beyond the corner


def test_bends_are_followed_and_dead_ends_stop(grid: StreetGraph) -> None:
    ep = grid.snap((200.0, 200.0))
    ep = grid.move(ep, EAST, 500.0)  # east, then round the bend north, then the dead end
    assert grid.point(ep) == pytest.approx((250.0, 250.0))
    assert grid.move(EdgePos(0, 1, 0.0), SOUTH, 50.0) == EdgePos(0, 1, 0.0)  # no street south


def test_snap_and_route(grid: StreetGraph) -> None:
    assert grid.point(grid.snap((130.0, 7.0))) == pytest.approx((130.0, 0.0))
    path = grid.route((0.0, 10.0), (200.0, 190.0))
    length = sum(math.dist(p, q) for p, q in itertools.pairwise(path))
    assert path[0] == pytest.approx((0.0, 10.0)) and path[-1] == pytest.approx((200.0, 190.0))
    assert length == pytest.approx(200 + 180)  # along x=0, y=100 (or 200), x=200


def test_route_follower_walks_the_route(grid: StreetGraph) -> None:
    follower = RouteFollower()
    pos = (0.0, 0.0)
    for _ in range(60):
        pos = follower.step(grid, pos, (200.0, 200.0), 10.0)
    assert pos == pytest.approx((200.0, 200.0))
    assert along([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 0.75) == pytest.approx((10.0, 5.0))


def osm_payload(bbox: list[float]) -> dict[str, object]:
    elements = [
        {"type": "node", "id": 1, "lat": 47.500, "lon": 19.050},
        {"type": "node", "id": 2, "lat": 47.500, "lon": 19.051},
        {"type": "node", "id": 3, "lat": 47.501, "lon": 19.051},
        {"type": "node", "id": 4, "lat": 47.600, "lon": 19.051},  # outside the area
        {"type": "node", "id": 5, "lat": 47.5005, "lon": 19.0530},
        {"type": "node", "id": 6, "lat": 47.5006, "lon": 19.0531},
        {"type": "way", "id": 10, "nodes": [1, 2, 3, 4]},
        {"type": "way", "id": 11, "nodes": [5, 6]},  # a separate scrap of street
    ]
    return {"version": STREETS_VERSION, "bbox": bbox, "elements": elements}


def test_read_and_build_from_osm(tmp_path: Path) -> None:
    path = tmp_path / "streets.json"
    path.write_text(json.dumps(osm_payload([47.49, 19.04, 47.61, 19.06])), encoding="utf-8")
    osm = read_streets(path, (47.495, 19.045, 47.505, 19.055))
    assert osm is not None
    area = PolygonArea(vertices=[(47.49, 19.04), (47.51, 19.04), (47.51, 19.06), (47.49, 19.06)])
    graph = StreetGraph.from_osm(*osm, lambda lat, lon: (lon * 1000, lat * 1000), area.contains)
    assert len(graph) == 3  # nodes 1-2-3: the outside node and the scrap are dropped
    assert read_streets(path, (47.0, 19.0, 47.5, 19.05)) is None  # not covered
    assert read_streets(tmp_path / "missing.json") is None
    path.write_text(json.dumps({"version": -1}), encoding="utf-8")
    assert read_streets(path) is None
    assert area_bbox(area) == (47.49, 19.04, 47.51, 19.06)


def test_download_streets_posts_the_query(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json={"elements": osm_payload([])["elements"]})

    bbox = (47.49, 19.04, 47.51, 19.06)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    path = download_streets(tmp_path / "osm" / "streets.json", bbox, client)
    assert "highway" in httpx.QueryParams(seen[0])["data"]
    assert httpx.QueryParams(seen[0])["data"] == overpass_query(bbox)
    assert read_streets(path, bbox) is not None


def test_walking_in_a_game_stays_on_the_streets(
    timetable: Timetable, fixture_network_config: NetworkConfig
) -> None:
    world = World(timetable, fixture_network_config.area)
    central = world.node_xy[world.tt.find_nodes("Central")[0]]
    cx, cy = central
    # A plus-shaped crossing at Central: one street east-west, one north-south.
    world.set_streets(
        StreetGraph(
            [(cx - 500, cy), (cx, cy), (cx + 500, cy), (cx, cy - 500), (cx, cy + 500)],
            [(0, 1), (1, 2), (3, 1), (1, 4)],
        )
    )
    game = LiveGame(world, LiveConfig(inspected_share=0.0), seed=1)
    game.hunters.agents = []
    game.dodger.pos = central
    game.keys = Keys(up=True, right=True)  # diagonal: must pick one street
    game.step(20)
    x, y = game.dodger.pos
    assert (x == pytest.approx(cx) and y > cy) or (y == pytest.approx(cy) and x > cx)
    assert math.dist(central, game.dodger.pos) == pytest.approx(20 * game.cfg.walk_speed_mps)
    assert game.status is Status.RUNNING


def test_a_tight_bend_is_walked_round_holding_one_key() -> None:
    # East 100 m, then a hairpin-ish bend: north-west 30 m, then north 100 m.
    graph = StreetGraph(
        [(0.0, 0.0), (100.0, 0.0), (80.0, 25.0), (80.0, 125.0)], [(0, 1), (1, 2), (2, 3)]
    )
    ep = graph.snap((0.0, 0.0))
    for _ in range(30):
        ep = graph.move(ep, EAST, 5.0)
    # Arrived at the bend heading east; the onward street turns back west-ish, so stop there.
    assert graph.point(ep) == pytest.approx((100.0, 0.0))
    for _ in range(40):
        ep = graph.move(ep, NORTH, 5.0)  # north takes the bend and then the street north
    assert graph.point(ep) == pytest.approx((80.0, 125.0))
